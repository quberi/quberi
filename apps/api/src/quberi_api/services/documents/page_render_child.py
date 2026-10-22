"""Render one page of a PDF to a JPEG. Runs in its own short-lived process.

    python -m quberi_api.services.documents.page_render_child <page> <width>

reads the PDF from stdin and writes the JPEG to stdout. A PDF is an untrusted file parsed by a
native library, so this is kept out of the API process: if the library crashes, hangs or eats
memory on a hostile file, only this child is lost. It has a memory and a CPU limit of its own, and
the parent also kills it after a timeout.

Exit status: 0 done, 2 no such page, 3 not a PDF this can read (corrupt, encrypted, empty).
"""

from __future__ import annotations

import io
import sys

# Pixels in the result, whatever the page's size: a page the size of a poster must not
# become a very large image.
MAX_PIXELS = 6_000_000
MAX_SCALE = 4.0
OK, NO_SUCH_PAGE, UNREADABLE = 0, 2, 3


def _limit_resources() -> None:
    try:
        import resource

        two_gb = 2 * 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (two_gb, two_gb))
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    except (ImportError, ValueError, OSError):
        # Not every platform allows it (macOS refuses RLIMIT_AS); the parent's timeout still holds.
        pass


def render(data: bytes, page: int, width: int) -> int:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
        count = len(pdf)
    except Exception:  # noqa: BLE001 - whatever the library raises, the file is unreadable
        return UNREADABLE
    if count <= 0:
        return UNREADABLE
    if page < 1 or page > count:
        return NO_SUCH_PAGE
    try:
        selected = pdf[page - 1]
        page_width, page_height = selected.get_size()
        if page_width <= 0 or page_height <= 0:
            return UNREADABLE
        scale = min(width / page_width, MAX_SCALE)
        # Keep the result under MAX_PIXELS.
        pixels = (page_width * scale) * (page_height * scale)
        if pixels > MAX_PIXELS:
            scale *= (MAX_PIXELS / pixels) ** 0.5
        image = selected.render(scale=scale).to_pil().convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=80)
    except Exception:  # noqa: BLE001
        return UNREADABLE
    sys.stdout.buffer.write(out.getvalue())
    sys.stdout.buffer.flush()
    return OK


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return UNREADABLE
    try:
        page, width = int(argv[1]), int(argv[2])
    except ValueError:
        return UNREADABLE
    _limit_resources()
    return render(sys.stdin.buffer.read(), page, width)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
