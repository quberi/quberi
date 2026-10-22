"""Tiny PDFs built by hand for tests, so no PDF-writing library is needed.

A page is a list of ``(x, y, size, text)`` draws, with ``y`` measured from the **top** of
the page (the PDF format measures from the bottom; this converts). Text uses the standard
Helvetica font, so it is ASCII only; that is enough to pin page numbers, block boundaries
and reading order, which is what the extractor adds. Reading Chinese is the PDF library's
job, not ours.

``scanned_pdf`` makes pages that are only a picture and have no text layer at all, which
is what a scan looks like to an extractor.
"""

from __future__ import annotations

import io
import zlib
from collections.abc import Sequence

PAGE_W, PAGE_H = 595, 842  # A4 in points

Draw = tuple[float, float, float, str]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(draws: Sequence[Draw]) -> bytes:
    ops = []
    for x, y_from_top, size, text in draws:
        y = PAGE_H - y_from_top - size
        ops.append(f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_escape(text)}) Tj ET")
    return "\n".join(ops).encode("latin-1")


def text_pdf(pages: Sequence[Sequence[Draw]]) -> bytes:
    """A PDF with a real text layer, one entry of ``pages`` per page."""

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    pages_id_placeholder = len(objects) + 1 + 2 * len(pages)  # id the /Pages object will get
    for draws in pages:
        stream = _content(draws)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page = add(
            (
                f"<< /Type /Page /Parent {pages_id_placeholder} 0 R "
                f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            ).encode()
        )
        page_ids.append(page)
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    assert pages_id == pages_id_placeholder
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    return _serialise(objects, catalog)


def scanned_pdf(images: Sequence[bytes]) -> bytes:
    """A PDF whose pages are only pictures (PNG/JPEG bytes in): no text layer."""

    from PIL import Image

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    page_ids = []
    first_pages_id = 3 * len(images) + 1  # three objects per page, then /Pages
    for data in images:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        raw = zlib.compress(img.tobytes())
        image = add(
            (
                f"<< /Type /XObject /Subtype /Image /Width {img.width} /Height {img.height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                f"/Length {len(raw)} >>\nstream\n"
            ).encode()
            + raw
            + b"\nendstream"
        )
        draw = f"q {PAGE_W} 0 0 {PAGE_H} 0 0 cm /Im0 Do Q".encode()
        content = add(b"<< /Length %d >>\nstream\n" % len(draw) + draw + b"\nendstream")
        page = add(
            (
                f"<< /Type /Page /Parent {first_pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Contents {content} 0 R /Resources << /XObject << /Im0 {image} 0 R >> >> >>"
            ).encode()
        )
        page_ids.append(page)
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    assert pages_id == first_pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    return _serialise(objects, catalog)


def _serialise(objects: list[bytes], root: int) -> bytes:
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R >>\n"
    out.write((trailer + f"startxref\n{xref}\n%%EOF\n").encode())
    return out.getvalue()


def blank_pdf(pages: int = 1) -> bytes:
    """Pages with no content at all (neither text nor picture)."""

    return text_pdf([[] for _ in range(pages)])
