"""Read the pages of a PDF that have no text layer (a scan, a photographed page).

The extractor (``extract.py``) is synchronous and cheap, so it only *flags* such pages.
Reading them is slow, asynchronous work that belongs here: draw the page as a picture, hand
it to the shared OCR module (``services/ai/ocr``), and turn the lines it returns into the
same kind of positioned blocks a text page produces, labelled ``ocr_block`` so that nothing
downstream presents recognised text as if it were the file's own.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from quberi_api.services.ai.ocr import OcrError, OcrLine, OCRProvider, OcrUnavailableError
from quberi_api.services.rag.extract import TextUnit

_log = logging.getLogger(__name__)

# The page is drawn so its longest side is about this many pixels (A4 at roughly 170 dpi).
# The OCR detector looks at no more than 2000 anyway, and a bigger picture only costs memory.
RENDER_LONGEST_SIDE = 2000
# Same rule as for text pages: a gap between two lines larger than this fraction of a line's
# height starts a new block.
_PARAGRAPH_GAP = 0.8
# Recognition is the slowest step of indexing, and the worker kills a job after 15 minutes
# (``WorkerSettings.job_timeout``). Real scanned pages took 6.5 to 10.8 seconds each on two
# CPU cores (a sparse synthetic page took 3 to 5, so do not size from those). A job killed
# in the middle loses every page already read, so reading stops on its own before the limit.
#
# The budget is the real constraint; the page cap is a second fence. 540 s leaves six of the
# fifteen minutes for chunking, embedding and writing, and is about 50 pages at the slow end.
# The clock is checked before each page, so the job can overrun by one page's own limit
# (``ocr_timeout_s``, 60 s): 600 s in the worst case. Raising that limit past about five
# minutes would defeat this budget.
OCR_TIME_BUDGET_S = 540.0
MAX_OCR_PAGES = 100


@dataclass(frozen=True)
class OcrPageOutcome:
    """What happened to the pages that needed reading."""

    read: int = 0
    failed: int = 0
    skipped: int = 0  # not tried: over the page limit, or the time budget ran out
    errors: tuple[str, ...] = ()
    out_of_time: bool = False


def blocks_from_lines(lines: Sequence[OcrLine]) -> list[str]:
    """Group recognised lines into paragraph-sized blocks by the gap between them."""

    ordered = sorted(lines, key=lambda ln: (round(ln.bbox[1], 1), ln.bbox[0]))
    blocks: list[list[str]] = []
    previous: OcrLine | None = None
    for line in ordered:
        text = line.text.strip()
        if not text:
            continue
        _, top, _, bottom = line.bbox
        height = max(bottom - top, 1.0)
        if previous is None or (top - previous.bbox[3]) > _PARAGRAPH_GAP * height:
            blocks.append([text])
        else:
            blocks[-1].append(text)
        previous = line
    return ["\n".join(block) for block in blocks]


def render_page_png(blob: bytes, page_number: int) -> bytes:
    """Draw one page (1-based) of the PDF as a PNG whose longest side is about 2000 px."""

    import pdfplumber

    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        page = pdf.pages[page_number - 1]
        longest_points = max(float(page.width), float(page.height), 1.0)
        # pdfplumber draws at 72 dpi * (resolution / 72) pixels per point.
        resolution = max(36.0, min(RENDER_LONGEST_SIDE / longest_points * 72.0, 300.0))
        image = page.to_image(resolution=resolution).original
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


async def read_scanned_pages(
    blob: bytes,
    units: Sequence[TextUnit],
    provider: OCRProvider,
    *,
    max_pages: int = MAX_OCR_PAGES,
    time_budget_s: float = OCR_TIME_BUDGET_S,
    clock: Callable[[], float] | None = None,
) -> tuple[list[TextUnit], OcrPageOutcome]:
    """Fill in the pages flagged ``needs_ocr``. Returns all units, in order, and a summary.

    Pages that already have text are left exactly as they are. A page that cannot be read
    (the engine is down, the page will not draw) stays without text and is counted, so the
    caller can say "12 of 40 pages could not be read" instead of failing the whole file.
    """

    wanted = [u.page for u in units if u.needs_ocr and u.page is not None]
    to_read = wanted[:max_pages]
    skipped = len(wanted) - len(to_read)
    recognised: dict[int, list[str]] = {}
    errors: list[str] = []
    failed = 0
    out_of_time = False
    loop = asyncio.get_running_loop()
    now = clock or loop.time
    started = now()
    for position, number in enumerate(to_read):
        if now() - started >= time_budget_s:
            # Stop before the job is killed: pages already read are kept.
            out_of_time = True
            skipped += len(to_read) - position
            break
        try:
            png = await loop.run_in_executor(None, render_page_png, blob, number)
            result = await provider.recognize(png)
        except OcrError as exc:
            failed += 1
            errors.append(f"page {number}: {exc}")
            # An engine that is down will not come back for the next page: stop asking, and
            # count the pages not tried as unread.
            if isinstance(exc, OcrUnavailableError):
                failed += len(to_read) - position - 1
                break
            continue
        except Exception as exc:  # noqa: BLE001 — a page that will not draw must not fail the file
            failed += 1
            errors.append(f"page {number}: {type(exc).__name__}: {exc}")
            continue
        recognised[number] = blocks_from_lines(result.lines)

    out: list[TextUnit] = []
    for unit in units:
        if not unit.needs_ocr:
            out.append(unit)
            continue
        blocks = recognised.get(unit.page or -1)
        if not blocks:
            out.append(unit)  # unread or genuinely empty: stays flagged, with no text
            continue
        out.extend(
            TextUnit(text=text, page=unit.page, unit_index=i, unit_label="ocr_block")
            for i, text in enumerate(blocks)
        )
    read = sum(1 for n in recognised if recognised[n])
    return out, OcrPageOutcome(
        read=read, failed=failed, skipped=skipped, errors=tuple(errors[:5]), out_of_time=out_of_time
    )
