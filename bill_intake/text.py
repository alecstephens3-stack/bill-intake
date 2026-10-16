"""Text out of an intake file.

Plain .txt files are read as they are. For a PDF, pypdf is used when it is installed; otherwise a
small stdlib reader pulls the strings shown by Tj / TJ / ' operators out of each content stream
(FlateDecode or uncompressed). That covers PDFs made by accounting and billing software, which is
most vendor email. It does not cover scans: a scan has no text layer, and `has_text_layer` comes
back False so the pipeline can send the file to a person (or, with a page renderer plugged in, to
the model as images) instead of guessing from nothing.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = (".pdf", ".txt")
MAX_PAGES = 3          # a bill is a page or two; a long statement past this is only cost


@dataclass
class Document:
    path: Path
    text: str = ""
    has_text_layer: bool = False
    page_count: int = 0
    images: list[bytes] = field(default_factory=list)   # filled only if a renderer is plugged in


def extract(path: Path) -> Document:
    path = Path(path)
    if path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        return Document(path, text, bool(text.strip()), 1)
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("not a PDF")
    try:
        text, pages = _with_pypdf(data)
    except ImportError:
        text, pages = _stdlib_pdf_text(data)
    return Document(path, text, bool(text.strip()), pages)


def _with_pypdf(data: bytes) -> tuple[str, int]:
    import io

    import pypdf  # optional

    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = reader.pages[:MAX_PAGES]
    return "\n".join((p.extract_text() or "") for p in pages), len(reader.pages)


_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)
_PAGE = re.compile(rb"/Type\s*/Page(?![s\w])")


def _stdlib_pdf_text(data: bytes) -> tuple[str, int]:
    pages = len(_PAGE.findall(data))
    chunks: list[str] = []
    for header, body in _STREAM.findall(data):
        if b"/FlateDecode" in header:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                continue
        elif b"/Filter" in header:
            continue                      # images and other filters carry no text
        text = _content_text(body)
        if text.strip():
            chunks.append(text)
        if len(chunks) >= MAX_PAGES:
            break
    return "\n".join(chunks), pages


_TOKENS = re.compile(rb"\((?:\\.|[^\\)])*\)|\[(?:\\.|[^\]\\])*\]|T\*|Tj|TJ|Td|TD|'|ET")
_LITERAL = re.compile(rb"\((?:\\.|[^\\)])*\)")
_ESCAPES = {b"n": "\n", b"r": "\r", b"t": "\t", b"b": "\b", b"f": "\f", b"(": "(", b")": ")", b"\\": "\\"}


def _content_text(stream: bytes) -> str:
    """Walk a content stream and keep the strings it shows, one line per line break operator."""
    out: list[str] = []
    line: list[str] = []
    pending: list[str] = []
    for tok in _TOKENS.findall(stream):
        if tok.startswith(b"("):
            pending = [_decode_literal(tok)]
        elif tok.startswith(b"["):
            pending = [_decode_literal(s) for s in _LITERAL.findall(tok)]
        elif tok in (b"Tj", b"TJ"):
            line.extend(pending)
            pending = []
        elif tok == b"'":
            out.append("".join(line))
            line = list(pending)
            pending = []
        elif tok in (b"T*", b"Td", b"TD", b"ET"):
            if line:
                out.append("".join(line))
                line = []
    if line:
        out.append("".join(line))
    return "\n".join(out)


def _decode_literal(tok: bytes) -> str:
    body = tok[1:-1]
    out, i = [], 0
    while i < len(body):
        c = body[i:i + 1]
        if c == b"\\" and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt]); i += 2; continue
            m = re.match(rb"[0-7]{1,3}", body[i + 1:i + 4])
            if m:
                out.append(chr(int(m.group(0), 8))); i += 1 + len(m.group(0)); continue
            i += 1
            continue
        out.append(c.decode("latin-1"))
        i += 1
    return "".join(out)


def list_intake(folder: Path) -> list[Path]:
    """The files a run reads: supported types in the intake folder itself (not subfolders), in name order."""
    return sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
