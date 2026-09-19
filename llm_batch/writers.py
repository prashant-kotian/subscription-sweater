"""Write collected answers to .txt / .xlsx / .docx.

The writer appends ONE entry per call and saves the file every time, so a
crash, Ctrl+C or "Stop" never loses answers that were already collected.
"""
from __future__ import annotations

import os
import re
import shutil

from .errors import InputError

SUPPORTED_OUT_EXTS = {".txt", ".xlsx", ".docx"}
BAR = "=" * 78
SEP = "-" * 78


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
        head = f"PROMPT {index}" + (f" [{label}]" if label else "")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(f"\n{BAR}\n{head}\n{BAR}\n{prompt}\n\nRESPONSE\n{SEP}\n{response}\n")

    def count(self) -> int:
        """How many PROMPT blocks are already in the file (for resume)."""
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            return len(re.findall(r"(?m)^PROMPT \d+( \[.+\])?$", f.read()))

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
        self._ws.append([label or index, prompt, response])
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
# .docx
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
