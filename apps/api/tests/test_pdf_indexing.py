"""A PDF is indexed with the page each passage is on, and scanned pages are read.

Real database, real indexer, real PDFs (built by hand), in-memory Qdrant. The embedding
model and the OCR engine are stood in for.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient
from sqlalchemy import update

from quberi_api.domain.documents.models import Document
from quberi_api.services.ai.ocr import OcrLine, OcrResult, OcrUnavailableError
from quberi_api.services.ai.ocr.models import ENGINE_NAME, ENGINE_VERSION, MODEL_NAME
from quberi_api.services.rag import indexer, pdf_ocr, retrieve
from quberi_api.services.rag.extract import ExtractError, TextUnit
from quberi_api.services.rag.indexer import OCR_ATTRIBUTE_KEY
from quberi_api.services.rag.pdf_ocr import blocks_from_lines, read_scanned_pages
from quberi_api.services.rag.vector_store import VectorStore
from tests.pdf_fixtures import scanned_pdf, text_pdf
from tests.test_image_indexing import _doc, _space

TEXT_MODEL = "test-embed"
DIM = 8


class FakeEmbedder:
    vector_dim = DIM

    async def embed(self, texts, **_kw):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


@pytest.fixture(autouse=True)
def embedding(monkeypatch):
    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=FakeEmbedder(), model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=DIM)
        )

    monkeypatch.setattr(indexer, "resolve_embedding_provider", _resolve)
    monkeypatch.setattr(retrieve, "resolve_embedding_provider", _resolve)


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
def blobs(monkeypatch):
    store: dict[str, bytes] = {}

    async def fake_download(storage_key: str) -> bytes:
        return store[storage_key]

    monkeypatch.setattr(indexer, "_download_blob", fake_download)
    return store


def _line(text, y=10, h=30):
    return OcrLine(text, 0.95, ((10, y), (400, y), (400, y + h), (10, y + h)))


class FakeOcr:
    """Answers a page by the number of the picture it is given (fixed per page)."""

    def __init__(self, pages=None, error=None):
        self.pages = pages or {}
        self.error = error
        self.calls = 0

    async def recognize(self, image):
        self.calls += 1
        if self.error:
            raise self.error
        lines = self.pages.get(self.calls, [])
        return OcrResult(
            lines=lines, engine=ENGINE_NAME, model=MODEL_NAME, version=ENGINE_VERSION,
            width=100, height=100,
        )


@pytest.fixture
def ocr(monkeypatch):
    engine = FakeOcr()
    monkeypatch.setattr("quberi_api.services.ai.ocr.get_ocr_provider", lambda: engine)
    return engine


def _png(color="white"):
    buf = io.BytesIO()
    Image.new("RGB", (100, 140), color).save(buf, format="PNG")
    return buf.getvalue()


async def _pdf(db, boot, blobs, data, name="contract.pdf"):
    space = await _space(db, boot)
    doc = await _doc(
        db, boot, space, name, mime="application/pdf", state="not_indexed",
        blobs=blobs, data=data,
    )
    await db.execute(update(Document).where(Document.id == doc.id).values(review_state="approved"))
    await db.commit()
    return space, doc


async def _points(qdrant):
    collection = VectorStore(qdrant, model_id=TEXT_MODEL, vector_dim=DIM).collection
    if not await qdrant.collection_exists(collection):
        return []
    found, _ = await qdrant.scroll(collection, limit=200, with_payload=True)
    return sorted((p.payload for p in found), key=lambda p: p["chunk_index"])


CONTRACT = text_pdf(
    [
        [(72, 72, 16, "Contract Title"), (72, 110, 11, "1. The supplier shall deliver"),
         (72, 124, 11, "the goods within 30 days.")],
        [(72, 72, 11, "Page two begins here."), (72, 200, 11, "Payment is due on receipt.")],
        [(72, 72, 11, "Page three: termination.")],
    ]
)


# -- text PDFs ---------------------------------------------------------------------------


async def test_a_pdf_is_indexed_with_the_pages_its_passages_are_on(
    db_session, org_and_owner, qdrant, blobs
):
    _, doc = await _pdf(db_session, org_and_owner, blobs, CONTRACT)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    (point,) = await _points(qdrant)  # small enough to be a single chunk
    assert (point["page_start"], point["page_end"]) == (1, 3)
    assert (point["unit_start"], point["unit_end"]) == (0, 0)
    assert point["unit_label"] == "text_block"
    assert "Contract Title" in point["text"] and "Page three: termination." in point["text"]


async def test_a_long_pdf_gets_a_page_range_per_chunk(db_session, org_and_owner, qdrant, blobs):
    pages = [
        [(72, 72, 11, f"Page {n} " + "word " * 40), (72, 200, 11, "x " * 200)] for n in range(1, 9)
    ]
    _, doc = await _pdf(db_session, org_and_owner, blobs, text_pdf(pages))

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    points = await _points(qdrant)
    assert len(points) > 2
    ranges = [(p["page_start"], p["page_end"]) for p in points]
    assert ranges[0][0] == 1 and ranges[-1][1] == 8
    assert all(a <= b for a, b in ranges)
    assert [r[0] for r in ranges] == sorted(r[0] for r in ranges)  # in document order


async def test_retrieval_returns_the_positions(db_session, org_and_owner, qdrant, blobs):
    space, doc = await _pdf(db_session, org_and_owner, blobs, CONTRACT)
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    hits = await VectorStore(qdrant, model_id=TEXT_MODEL, vector_dim=DIM).search(
        query_vector=[1.0] + [0.0] * (DIM - 1), space_ids=[space.id],
        organization_id=org_and_owner.organization_id,
    )
    assert [(h.page_start, h.page_end, h.unit_label) for h in hits] == [(1, 3, "text_block")]


async def test_a_text_file_has_line_numbers_and_no_pages(
    db_session, org_and_owner, qdrant, blobs
):
    space = await _space(db_session, org_and_owner)
    doc = await _doc(db_session, org_and_owner, space, "notes.txt", mime="text/plain",
                     state="not_indexed", blobs=blobs, data=b"plain words " * 20)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(review_state="approved")
    )
    await db_session.commit()
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    (point,) = await _points(qdrant)
    # No pages in a text file; its place is the lines (here the file's only line).
    assert "page_start" not in point
    assert (point["unit_label"], point["unit_start"], point["unit_end"]) == ("line", 1, 1)


# -- scanned pages -----------------------------------------------------------------------


async def test_a_scanned_pdf_is_read_and_its_text_carries_page_and_the_recognised_label(
    db_session, org_and_owner, qdrant, blobs, ocr
):
    ocr.pages = {
        1: [_line("Delivery within 30 days", 10), _line("Penalty 5 percent", 200)],
        2: [_line("Signed by both parties", 10)],
    }
    _, doc = await _pdf(db_session, org_and_owner, blobs, scanned_pdf([_png(), _png("grey")]))

    result = await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    assert result.chunk_count >= 1 and ocr.calls == 2
    (point,) = await _points(qdrant)
    assert (point["page_start"], point["page_end"]) == (1, 2)
    assert point["unit_label"] == "ocr_block"  # never presented as the file's own text
    assert "Delivery within 30 days" in point["text"] and "Signed by both parties" in point["text"]
    await db_session.refresh(doc)
    assert doc.attributes[OCR_ATTRIBUTE_KEY]["pages_read"] == 2
    assert doc.attributes[OCR_ATTRIBUTE_KEY]["pages_unread"] == 0


async def test_a_mixed_pdf_reads_only_the_pages_that_have_no_text(
    db_session, org_and_owner, qdrant, blobs, ocr
):
    ocr.pages = {1: [_line("Recognised from the picture")]}
    data = text_pdf(
        [[(72, 72, 12, "Page one has real text.")], [], [(72, 72, 12, "Page three too.")]]
    )
    _, doc = await _pdf(db_session, org_and_owner, blobs, data)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    assert ocr.calls == 1  # only the blank middle page
    (point,) = await _points(qdrant)
    assert (point["page_start"], point["page_end"]) == (1, 3)
    assert point["unit_label"] == "ocr_block"  # one recognised page taints the chunk
    text = point["text"]
    assert text.index("Page one") < text.index("Recognised") < text.index("Page three")


async def test_a_scan_that_cannot_be_read_fails_with_a_plain_reason_not_an_empty_index(
    db_session, org_and_owner, qdrant, blobs, monkeypatch
):
    from quberi_api.services.ai.ocr import OcrUnavailableError as Unavailable

    def off():
        raise Unavailable("OCR is turned off on this server")

    monkeypatch.setattr("quberi_api.services.ai.ocr.get_ocr_provider", off)
    _, doc = await _pdf(db_session, org_and_owner, blobs, scanned_pdf([_png()]))

    with pytest.raises(ExtractError):
        await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    await db_session.refresh(doc)
    assert doc.indexing_state == "failed"
    assert "no text layer" in doc.indexing_error and "turned off" in doc.indexing_error
    assert await _points(qdrant) == []


async def test_a_mixed_pdf_is_still_indexed_when_ocr_is_down_and_says_what_was_not_read(
    db_session, org_and_owner, qdrant, blobs, ocr
):
    ocr.error = OcrUnavailableError("the OCR engine was killed by the system")
    data = text_pdf(
        [[(72, 72, 12, "Page one has real text.")], [], [], [(72, 72, 12, "Page four.")]]
    )
    _, doc = await _pdf(db_session, org_and_owner, blobs, data)

    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)

    (point,) = await _points(qdrant)
    assert point["unit_label"] == "text_block" and "Page one" in point["text"]
    await db_session.refresh(doc)
    state = doc.attributes[OCR_ATTRIBUTE_KEY]
    assert state["pages_read"] == 0 and state["pages_unread"] + state["pages_failed"] >= 2
    assert doc.indexing_state == "indexed"
    assert "killed by the system" in " ".join(state["errors"])


async def test_the_other_attributes_survive_the_ocr_record(
    db_session, org_and_owner, qdrant, blobs, ocr
):
    ocr.pages = {1: [_line("Some recognised words here for the page")]}
    _, doc = await _pdf(db_session, org_and_owner, blobs, scanned_pdf([_png()]))
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(attributes={"owner_note": "keep"})
    )
    await db_session.commit()
    await indexer._run_pipeline(db_session, qdrant=qdrant, document_id=doc.id)
    await db_session.refresh(doc)
    assert doc.attributes["owner_note"] == "keep" and OCR_ATTRIBUTE_KEY in doc.attributes


# -- reading the pages (unit level) ------------------------------------------------------


def test_recognised_lines_are_grouped_into_blocks_by_the_gap_between_them():
    lines = [_line("first", 0), _line("same block", 34), _line("new block", 200)]
    assert blocks_from_lines(lines) == ["first\nsame block", "new block"]
    assert blocks_from_lines([]) == []
    assert blocks_from_lines([_line("   ", 0)]) == []


async def test_only_flagged_pages_are_touched_and_order_is_kept():
    units = [
        TextUnit("real text", 1, 0),
        TextUnit("", 2, 0, needs_ocr=True),
        TextUnit("more real text", 3, 0),
    ]
    engine = FakeOcr(pages={1: [_line("a", 0), _line("b", 300)]})
    out, summary = await read_scanned_pages(CONTRACT, units, engine)
    assert [(u.page, u.unit_index, u.unit_label, u.text) for u in out] == [
        (1, 0, "text_block", "real text"),
        (2, 0, "ocr_block", "a"),
        (2, 1, "ocr_block", "b"),
        (3, 0, "text_block", "more real text"),
    ]
    assert (summary.read, summary.failed) == (1, 0)


async def test_a_page_that_reads_as_empty_stays_flagged_and_is_not_an_error():
    units = [TextUnit("", 1, 0, needs_ocr=True)]
    out, summary = await read_scanned_pages(CONTRACT, units, FakeOcr(pages={1: []}))
    assert out[0].needs_ocr and out[0].text == ""
    assert (summary.read, summary.failed) == (0, 0)


async def test_when_the_engine_is_down_the_rest_of_the_pages_are_not_tried():
    units = [TextUnit("", n, 0, needs_ocr=True) for n in (1, 2, 3, 4)]
    engine = FakeOcr(error=OcrUnavailableError("down"))
    out, summary = await read_scanned_pages(CONTRACT, units, engine)
    assert engine.calls == 1  # not four
    assert summary.failed == 4 and summary.read == 0
    assert all(u.needs_ocr for u in out)


async def test_pages_past_the_limit_are_reported_not_silently_dropped():
    units = [TextUnit("", n, 0, needs_ocr=True) for n in (1, 2, 3)]
    engine = FakeOcr(pages={1: [_line("one")], 2: [_line("two")]})
    out, summary = await read_scanned_pages(CONTRACT, units, engine, max_pages=2)
    assert (summary.read, summary.skipped) == (2, 1)
    assert [u.text for u in out] == ["one", "two", ""]


TEN_PAGES = text_pdf([[(72, 72, 11, f"page {n}")] for n in range(1, 11)])


class Clock:
    """A clock that moves only when told to, so a time budget can be tested without waiting."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowOcr(FakeOcr):
    """Each page takes ``seconds`` of the fake clock."""

    def __init__(self, clock, seconds, pages=None):
        super().__init__(pages=pages)
        self.clock, self.seconds = clock, seconds

    async def recognize(self, image):
        self.clock.now += self.seconds
        return await super().recognize(image)


async def test_reading_stops_before_the_job_would_be_killed_and_keeps_what_was_read():
    clock = Clock()
    units = [TextUnit("", n, 0, needs_ocr=True) for n in range(1, 11)]
    engine = SlowOcr(clock, seconds=10, pages={n: [_line(f"page {n} words")] for n in range(1, 11)})

    out, summary = await read_scanned_pages(TEN_PAGES, units, engine, time_budget_s=35, clock=clock)

    assert engine.calls == 4  # pages at t=0, 10, 20, 30; at t=40 the budget is spent
    assert summary.read == 4 and summary.out_of_time and summary.skipped == 6
    assert [u.text for u in out[:4]] == [f"page {n} words" for n in range(1, 5)]
    assert all(u.needs_ocr and not u.text for u in out[4:])  # unread pages stay flagged


async def test_a_job_that_finishes_inside_the_budget_is_not_marked_out_of_time():
    clock = Clock()
    units = [TextUnit("", n, 0, needs_ocr=True) for n in (1, 2)]
    engine = SlowOcr(clock, seconds=5, pages={1: [_line("a")], 2: [_line("b")]})
    _, summary = await read_scanned_pages(CONTRACT, units, engine, time_budget_s=540, clock=clock)
    assert summary.read == 2 and not summary.out_of_time and summary.skipped == 0


async def test_the_default_budget_leaves_room_inside_the_workers_time_limit():
    from quberi_api.workers.rag import WorkerSettings

    worst_overrun = pdf_ocr.OCR_TIME_BUDGET_S + 60  # one more page at the engine's own timeout
    assert worst_overrun < WorkerSettings.job_timeout - 120


def test_a_page_is_drawn_with_its_longest_side_near_the_target():
    png = pdf_ocr.render_page_png(CONTRACT, 1)
    width, height = Image.open(io.BytesIO(png)).size
    assert abs(max(width, height) - pdf_ocr.RENDER_LONGEST_SIDE) <= 4
    assert height > width  # A4 portrait stays portrait


# -- a page cannot make the worker draw something enormous -------------------------------


def _pdf_with_page_size(width, height):
    import tests.pdf_fixtures as fixtures

    saved = (fixtures.PAGE_W, fixtures.PAGE_H)
    fixtures.PAGE_W, fixtures.PAGE_H = width, height
    try:
        return text_pdf([[(72, 72, 11, "x")]])
    finally:
        fixtures.PAGE_W, fixtures.PAGE_H = saved


@pytest.mark.parametrize(
    "width, height",
    [(595, 842), (842, 595), (5000, 7000), (14400, 14400), (19000, 300)],
)
def test_the_picture_is_never_bigger_than_the_target_however_big_the_page_claims_to_be(
    width, height
):
    png = pdf_ocr.render_page_png(_pdf_with_page_size(width, height), 1)
    drawn = Image.open(io.BytesIO(png)).size
    assert max(drawn) <= pdf_ocr.RENDER_LONGEST_SIDE + 8, drawn


@pytest.mark.parametrize("width, height", [(20001, 100), (200000, 200000), (0.5, 842)])
def test_a_page_that_declares_an_impossible_size_is_refused_not_drawn(width, height):
    with pytest.raises(pdf_ocr.PageNotReadable, match="impossible size"):
        pdf_ocr.render_page_png(_pdf_with_page_size(width, height), 1)


async def test_one_page_that_cannot_be_drawn_does_not_stop_the_others():
    """It is a failure of that page, not of the engine, so the rest are still read."""
    blob = _pdf_with_page_size(595, 842)
    units = [TextUnit("", n, 0, needs_ocr=True) for n in (1, 2)]
    engine = FakeOcr(pages={1: [_line("only page one exists")]})
    # Page 2 does not exist in this one-page file, so drawing it fails.
    out, summary = await read_scanned_pages(blob, units, engine)
    assert engine.calls == 1
    assert (summary.read, summary.failed) == (1, 1)
    assert out[0].text == "only page one exists" and out[1].needs_ocr


def test_a_large_page_is_drawn_at_a_low_resolution_not_drawn_big_and_shrunk(monkeypatch):
    """The first defence: do not spend the memory. Drawing 14400 points at 36 dpi alone
    makes a 7200 px picture, which the later cut-down would only shrink after it exists."""
    import pdfplumber.page

    asked = []
    real = pdfplumber.page.Page.to_image

    def spy(self, *args, **kwargs):
        asked.append(kwargs.get("resolution"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", spy)
    pdf_ocr.render_page_png(_pdf_with_page_size(14400, 14400), 1)
    resolution = asked[0]
    # pixels = points * resolution / 72
    assert 14400 * resolution / 72 <= pdf_ocr.RENDER_LONGEST_SIDE + 8, resolution


def test_the_cut_down_is_a_second_guard_that_still_works_on_its_own(monkeypatch):
    """If the renderer hands back something bigger than asked for, it is shrunk."""
    import pdfplumber.page

    class Huge:
        def __init__(self):
            self.original = Image.new("RGB", (6000, 3000), "white")

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", lambda self, *a, **k: Huge())
    png = pdf_ocr.render_page_png(_pdf_with_page_size(595, 842), 1)
    assert max(Image.open(io.BytesIO(png)).size) <= pdf_ocr.RENDER_LONGEST_SIDE + 8
