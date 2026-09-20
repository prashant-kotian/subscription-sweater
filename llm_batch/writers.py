"""Write collected answers to .txt / .xlsx / .docx.

Each prompt occupies ONE entry. The file is saved after every entry, so a
crash, Ctrl+C or "Stop" never loses answers that were already collected.

Re-running a prompt (e.g. retrying a failed one) REPLACES that prompt's
existing entry in place instead of stacking a duplicate block on top of the
old attempts -- so the output file always holds exactly one entry per prompt,
reflecting the latest attempt. (.txt and .xlsx do this; .docx appends.)
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile

from .errors import InputError

SUPPORTED_OUT_EXTS = {".txt", ".xlsx", ".docx"}
BAR = "=" * 78
SEP = "-" * 78
# Matches a .txt block header: "==...==\nPROMPT <n> [<label>]\n==...=="
_TXT_HEAD_RE = re.compile(
    r"^" + re.escape(BAR) + r"\nPROMPT (\d+)(?: \[([^\]]*)\])?\n" + re.escape(BAR) + r"\n",
    re.MULTILINE,
)


# Inline-image chip artifact: Gemini's web UI renders an image reference
# inside an answer as a chip whose copied text is just the extension ("PNG"
# on its own line). Strip such WHOLE lines so the saved answer reads clean.
_IMAGE_CHIP_RE = re.compile(r"(?im)^\s*(png|jpe?g|webp|gif|bmp|svg)\s*$")


def clean_response(text: str) -> str:
    """Remove image-chip artifact lines from a captured response (all other
    text -- including words like 'PNG' inside sentences -- is untouched)."""
    if not text:
        return text
    return _IMAGE_CHIP_RE.sub("", text).strip("\n")


def make_writer(path: str, log=print):
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        path += ".txt"
        ext = ".txt"
    if ext not in SUPPORTED_OUT_EXTS:
        raise InputError(f"Unsupported output type '{ext}'. Use .txt, .xlsx or .docx.")
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    if ext == ".txt":
        return TxtWriter(path, log)
    if ext == ".xlsx":
        return XlsxWriter(path, log)
    return DocxWriter(path, log)


# --------------------------------------------------------------------------- #
# .txt
# --------------------------------------------------------------------------- #
class TxtWriter:
    def __init__(self, path: str, log=print):
        self.path = path
        self.log = log

    def add(self, index: int, prompt: str, response: str, label: str = "") -> None:
        response = clean_response(response)
        blocks = self._parse()
        if blocks is None:
            # Could not parse the existing file with confidence -- keep the
            # old, always-safe append-only behaviour rather than risk data.
            self._append(index, prompt, response, label)
            return
        # Upsert by prompt index: the FIRST block for this index takes the new
        # content (keeping its position in the file); any stale duplicate
        # blocks for the same index are dropped, so a file always holds one
        # entry per prompt.
        seen = False
        kept = []
        for b in blocks:
            if b[0] == index:
                if not seen:
                    kept.append([index, label, prompt, response])
                    seen = True
            else:
                kept.append(b)
        if not seen:
            kept.append([index, label, prompt, response])
        out = []
        for idx, lab, p, r in kept:
            head = f"PROMPT {idx}" + (f" [{lab}]" if lab else "")
            out.append(f"\n{BAR}\n{head}\n{BAR}\n{p}\n\nRESPONSE\n{SEP}\n{r}\n")
        self._atomic_write("".join(out))

    def _parse(self):
        """Parse the file into [(index, label, prompt, response), ...] in file
        order. Returns None if the file cannot be read/parsed with confidence
        (caller then falls back to append-only)."""
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return None
        matches = list(_TXT_HEAD_RE.finditer(text))
        if not matches:
            return []
        blocks = []
        for i, m in enumerate(matches):
            s = max(0, m.start() - 1)  # include this block's leading newline
            e = (matches[i + 1].start() - 1) if i + 1 < len(matches) else len(text)
            body = text[s:e]
            mb = re.match(
                r"\n?" + re.escape(BAR) + r"\nPROMPT (\d+)(?: \[([^\]]*)\])?\n"
                + re.escape(BAR) + r"\n(.*)\Z",
                body, re.DOTALL,
            )
            if not mb:
                return None
            rest = mb.group(3)
            marker = "\n\nRESPONSE\n" + SEP + "\n"
            if marker not in rest:
                return None
            p, r = rest.split(marker, 1)
            blocks.append([int(mb.group(1)), mb.group(2) or "", p, r.rstrip("\n")])
        return blocks

    def _append(self, index: int, prompt: str, response: str, label: str = "") -> None:
        head = f"PROMPT {index}" + (f" [{label}]" if label else "")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"\n{BAR}\n{head}\n{BAR}\n{prompt}\n\nRESPONSE\n{SEP}\n{response}\n")

    def _atomic_write(self, content: str) -> None:
        # write to a temp file then rename, so a crash mid-write can't corrupt
        # the already-collected answers
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".llmbatch_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def count(self) -> int:
        """How many PROMPT blocks are already in the file (for resume)."""
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            return len(re.findall(r"(?m)^PROMPT \d+( \[.+?\])?$", f.read()))

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# .xlsx
# --------------------------------------------------------------------------- #
class XlsxWriter:
    HEADER = ["#", "Prompt", "Response"]

    def __init__(self, path: str, log=print):
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Alignment, Font

        self.path = path
        self.log = log
        self._wrap = Alignment(wrap_text=True, vertical="top")
        self._wb = None
        self._ws = None

        if os.path.exists(path):
            try:
                wb = load_workbook(path)
                ws = wb.active
                row1 = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
                first = [str(c).strip().lower() for c in row1 if c is not None] if row1 else []
                if first == [h.lower() for h in self.HEADER]:
                    self._wb = wb
                    self._ws = ws
                    return
                wb.close()
            except Exception:
                pass
            self._backup()

        self._wb = Workbook()
        self._ws = self._wb.active
        self._ws.title = "Answers"
        self._ws.append(self.HEADER)
        for c in self._ws[1]:
            c.font = Font(bold=True)
        self._ws.column_dimensions["A"].width = 6
        self._ws.column_dimensions["B"].width = 60
        self._ws.column_dimensions["C"].width = 100
        self._save()

    def _backup(self) -> None:
        bak = self.path + ".bak"
        try:
            shutil.copy2(self.path, bak)
            self.log(f"Output file was not in the expected format — kept a copy at {bak} and starting fresh.")
        except Exception:
            pass

    def add(self, index: int, prompt: str, response: str, label: str = "") -> None:
        # column A shows the prompt's own ID when one was captured (e.g. GZ-01)
        response = clean_response(response)
        key = label or index
        keystr = str(key).strip()
        # Upsert: replace the first existing row with this key, drop any stale
        # duplicate rows for it.
        replaced = False
        to_remove = []
        for r in range(2, self._ws.max_row + 1):
            cell = self._ws.cell(row=r, column=1)
            if cell.value is None:
                continue
            if str(cell.value).strip() == keystr:
                if not replaced:
                    cell.value = key
                    self._ws.cell(row=r, column=2).value = prompt
                    self._ws.cell(row=r, column=3).value = response
                    for c in self._ws[r]:
                        c.alignment = self._wrap
                    replaced = True
                else:
                    to_remove.append(r)
        for r in reversed(to_remove):
            self._ws.delete_rows(r, 1)
        if not replaced:
            self._ws.append([key, prompt, response])
            for c in self._ws[self._ws.max_row]:
                c.alignment = self._wrap
        self._save()

    def count(self) -> int:
        return max(0, self._ws.max_row - 1)

    def _save(self) -> None:
        self._wb.save(self.path)

    def close(self) -> None:
        if self._wb is not None:
            try:
                self._save()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# .docx  (append-only: re-running a prompt adds another "Prompt N" section)
# --------------------------------------------------------------------------- #
class DocxWriter:
    def __init__(self, path: str, log=print):
        from docx import Document

        self.path = path
        self.log = log
        self._doc = None

        if os.path.exists(path):
            try:
                d = Document(path)
                first_heading = next(
                    (p.text for p in d.paragraphs if p.style.name.startswith("Heading")),
                    "",
                )
                if first_heading.strip().lower().startswith("prompt "):
                    self._doc = d
                    return
            except Exception:
                pass
            try:
                shutil.copy2(path, path + ".bak")
                self.log(f"Output file was not in the expected format — kept a copy at {path}.bak and starting fresh.")
            except Exception:
                pass

        self._doc = Document()
        self._doc.add_heading("LLM Batch Responses", level=0)

    def _add_block(self, text: str) -> None:
        for i, para in enumerate(text.split("\n")):
            self._doc.add_paragraph(para)

    def add(self, index: int, prompt: str, response: str, label: str = "") -> None:
        d = self._doc
        response = clean_response(response)
        head = f"Prompt {index}" + (f" ({label})" if label else "")
        d.add_heading(head, level=1)
        self._add_block(prompt)
        d.add_heading("Response", level=2)
        self._add_block(response)
        if index % 5 == 0:
            self.close()

    def count(self) -> int:
        if not os.path.exists(self.path):
            return 0
        from docx import Document

        try:
            d = Document(self.path)
        except Exception:
            return 0
        return sum(
            1
            for p in d.paragraphs
            if p.style.name == "Heading 1" and p.text.strip().startswith("Prompt ")
        )

    def close(self) -> None:
        if self._doc is not None:
            try:
                self._doc.save(self.path)
            except Exception:
                pass
