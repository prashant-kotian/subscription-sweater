"""Read prompts from .txt / .csv / .xlsx / .docx / .doc files.

Every function returns a list of prompt strings (empty entries removed).
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import tempfile

from .errors import InputError

SUPPORTED_EXTS = {".txt", ".csv", ".xlsx", ".xls", ".docx", ".doc"}

_HEADER_HINTS = {
    "prompt", "prompts", "question", "questions", "input", "inputs",
    "query", "queries", "q", "text", "message", "task", "instruction",
}


def read_prompts(path: str, txt_mode: str = "auto") -> list[str]:
    """Load prompts from *path* (extension decides the parser)."""
    return read_prompts_labeled(path, txt_mode)[0]


def read_prompts_labeled(path: str, txt_mode: str = "auto") -> "tuple[list[str], list[str]]":
    """Load prompts from *path*; returns (prompts, labels).

    In marked-block mode the label is the short ID line sitting directly above
    the marker (e.g. ``GZ-01``); otherwise labels are empty strings.
    """
    if not os.path.exists(path):
        raise InputError(f"Input file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except Exception as e:
            raise InputError(f"Could not read text file: {e}")
        return _read_txt_file(text, txt_mode)
    if ext == ".csv":
        p = _read_csv(path)
        return p, [""] * len(p)
    if ext in (".xlsx", ".xls"):
        p = _read_excel(path)
        return p, [""] * len(p)
    if ext == ".docx":
        p = _read_docx(path)
        return p, [""] * len(p)
    if ext == ".doc":
        p = _read_doc(path)
        return p, [""] * len(p)
    raise InputError(
        f"Unsupported input type '{ext}'. Use .txt, .csv, .xlsx, .docx or .doc."
    )


# --------------------------------------------------------------------------- #
# .txt
# --------------------------------------------------------------------------- #
# Benchmark-style files wrap the real prompt between explicit markers;
# everything else in the file (IDs, "reference only" notes, batch headers)
# must NOT be sent to the LLM:
#
#     GZ-01
#     [reference only, do not paste -- …]
#
#     >>> PASTE BELOW >>>
#     <the actual prompt>
#     <<< PASTE ABOVE <<<
MARKED_RE = re.compile(
    r">>>\s*PASTE\s+BELOW\s*>>>\s*\n(.*?)\n\s*<<<\s*PASTE\s+ABOVE\s*<<<",
    re.S,
)


def extract_marked_blocks(text: str) -> "list[tuple[str, str]]":
    """Return [(label, prompt)] for every >>> PASTE BELOW >>> block.

    Label = the nearest short ID-like line above the marker (e.g. ``GZ-01``),
    scanning back a few lines past blank/reference lines; "" when none found.
    """
    out: list[tuple[str, str]] = []
    for m in MARKED_RE.finditer(text):
        prompt = m.group(1).strip()
        if not prompt:
            continue
        label = ""
        pre_lines = text[: m.start()].rstrip().splitlines()
        for ln in reversed(pre_lines[-6:]):  # scan the few lines ABOVE the marker
            ln = ln.strip()
            if re.fullmatch(r"[A-Za-z0-9_\-]{1,24}", ln):
                label = ln
                break
        out.append((label, prompt))
    return out


def _read_txt_file(text: str, mode: str) -> "tuple[list[str], list[str]]":
    """Parse txt *text*; returns (prompts, labels)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # marked blocks (forced, or auto-detected): ONLY the marked sections count
    if mode == "marked" or (mode == "auto" and MARKED_RE.search(text)):
        blocks = extract_marked_blocks(text)
        if blocks:
            return [p for _, p in blocks], [l for l, _ in blocks]

    if mode == "line":
        ps = [ln.strip() for ln in text.split("\n") if ln.strip()]
        return ps, [""] * len(ps)

    # blank-line separated blocks
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1 and "\n" in blocks[0]:
        # no blank-line separators at all: one prompt per line
        ps = [ln.strip() for ln in text.split("\n") if ln.strip()]
        return ps, [""] * len(ps)
    return blocks, [""] * len(blocks)


def _read_txt(path: str, mode: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except Exception as e:
        raise InputError(f"Could not read text file: {e}")
    return _read_txt_file(text, mode)[0]


# --------------------------------------------------------------------------- #
# .csv
# --------------------------------------------------------------------------- #
def _read_csv(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            rows = [row for row in csv.reader(f) if any(c.strip() for c in row)]
    except Exception as e:
        raise InputError(f"Could not read CSV file: {e}")
    if not rows:
        return []
    col, skip_header = _pick_column(rows)
    prompts = [
        str(r[col]).strip()
        for r in (rows[1:] if skip_header else rows)
        if col < len(r)
    ]
    return [p for p in prompts if p]


def _pick_column(rows: list[list[str]]) -> tuple[int, bool]:
    """Return (column_index, skip_header_row) for a table of strings."""
    if not rows:
        return 0, False
    first = rows[0]
    for idx, cell in enumerate(first):
        if str(cell).strip().lower() in _HEADER_HINTS:
            return idx, True
    for idx in range(len(first)):
        if any(str(r[idx]).strip() for r in rows if idx < len(r)):
            return idx, False
    return 0, False


# --------------------------------------------------------------------------- #
# .xlsx
# --------------------------------------------------------------------------- #
def _read_excel(path: str) -> list[str]:
    if os.path.splitext(path)[1].lower() == ".xls":
        raise InputError(
            "Old .xls files are not supported — open the file in Excel and "
            "'Save As' .xlsx (or .csv), then pick that file."
        )
    try:
        import openpyxl
    except ImportError:
        raise InputError("openpyxl is needed for Excel files. Install: pip install openpyxl")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise InputError(f"Could not read Excel file: {e}")
    try:
        ws = wb.active
        rows = [
            [("" if c is None else str(c)) for c in row]
            for row in ws.iter_rows(values_only=True)
        ]
    finally:
        wb.close()
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return []
    col, skip_header = _pick_column(rows)
    prompts = [
        str(r[col]).strip()
        for r in (rows[1:] if skip_header else rows)
        if col < len(r)
    ]
    return [p for p in prompts if p]


# --------------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------------- #
def _read_docx(path: str) -> list[str]:
    try:
        import docx
    except ImportError:
        raise InputError("python-docx is needed for Word files. Install: pip install python-docx")
    try:
        d = docx.Document(path)
    except Exception as e:
        raise InputError(f"Could not read Word file: {e}")

    lines = [p.text.strip() for p in d.paragraphs]
    if any(ln == "" for ln in lines):
        # blank paragraphs act as separators between (possibly multi-line) prompts
        blocks: list[str] = []
        cur: list[str] = []
        for ln in lines:
            if ln:
                cur.append(ln)
            elif cur:
                blocks.append("\n".join(cur))
                cur = []
        if cur:
            blocks.append("\n".join(cur))
        if blocks:
            return blocks
    else:
        # no blank paragraphs: every non-empty paragraph is one prompt
        out = [ln for ln in lines if ln]
        if out:
            return out

    # no usable paragraphs -> try the first table
    for table in d.tables:
        rows = [[c.text for c in r.cells] for r in table.rows]
        col, skip_header = _pick_column(rows)
        vals = [r[col].strip() for r in (rows[1:] if skip_header else rows)]
        vals = [v for v in vals if v]
        if vals:
            return vals
    return []


# --------------------------------------------------------------------------- #
# .doc (old binary Word) — convert with whatever converter is installed
# --------------------------------------------------------------------------- #
def _read_doc(path: str) -> list[str]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "docx",
                     "--outdir", tmp, path],
                    capture_output=True, timeout=180, check=False,
                )
                out = os.path.join(
                    tmp, os.path.splitext(os.path.basename(path))[0] + ".docx"
                )
                if os.path.exists(out):
                    return _read_docx(out)
        except Exception:
            pass

    antiword = shutil.which("antiword")
    if antiword:
        try:
            r = subprocess.run([antiword, path], capture_output=True, timeout=60, check=False)
            if r.returncode == 0:
                text = r.stdout.decode("utf-8", "replace")
                blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
                return blocks or [ln.strip() for ln in text.splitlines() if ln.strip()]
        except Exception:
            pass

    raise InputError(
        "Old binary .doc files need LibreOffice or antiword installed to be read.\n"
        "Easiest fix: open the file in Word / Google Docs and 'Save as' .docx "
        "or .txt, then pick that file here."
    )
