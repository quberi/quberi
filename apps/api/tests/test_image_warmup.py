"""The picture model's text half is loaded at start-up only where pictures exist."""

from __future__ import annotations

import pytest

from quberi_api.core.config import get_settings
from quberi_api.services.rag import image_warmup
from tests.test_image_indexing import _doc, _space


class Spy:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("model could not load")
        return [[0.0]]


@pytest.fixture
def spy(monkeypatch):
    instance = Spy()
    monkeypatch.setattr(image_warmup, "get_image_embedder", lambda *_a, **_k: instance)
    return instance


class Factory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


async def test_it_warms_the_model_when_a_picture_is_searchable(db_session, org_and_owner, spy):
    space = await _space(db_session, org_and_owner)
    await _doc(db_session, org_and_owner, space, "a.png", state="indexed")
    assert await image_warmup.warm_picture_search(Factory(db_session), get_settings()) is True
    assert spy.calls == [["warm up"]]


@pytest.mark.parametrize(
    "state, mime, status",
    [
        ("not_indexed", "image/png", "uploaded"),  # not searchable yet
        ("indexed", "application/pdf", "uploaded"),  # not a picture
        ("indexed", "image/png", "archived"),  # gone
    ],
)
async def test_it_does_not_warm_the_model_for_pictures_nobody_can_search(
    db_session, org_and_owner, spy, state, mime, status
):
    space = await _space(db_session, org_and_owner)
    await _doc(db_session, org_and_owner, space, "x", mime=mime, state=state, status=status)
    assert await image_warmup.warm_picture_search(Factory(db_session), get_settings()) is False
    assert spy.calls == []


async def test_it_costs_nothing_when_there_are_no_pictures_at_all(db_session, spy):
    assert await image_warmup.warm_picture_search(Factory(db_session), get_settings()) is False
    assert spy.calls == []


async def test_it_does_nothing_when_picture_search_is_switched_off(
    db_session, org_and_owner, spy, monkeypatch
):
    space = await _space(db_session, org_and_owner)
    await _doc(db_session, org_and_owner, space, "a.png", state="indexed")
    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    assert await image_warmup.warm_picture_search(Factory(db_session), get_settings()) is False
    assert spy.calls == []


async def test_a_failed_warm_up_is_swallowed(db_session, org_and_owner, monkeypatch):
    space = await _space(db_session, org_and_owner)
    await _doc(db_session, org_and_owner, space, "a.png", state="indexed")
    broken = Spy(fail=True)
    monkeypatch.setattr(image_warmup, "get_image_embedder", lambda *_a, **_k: broken)
    assert await image_warmup.warm_picture_search(Factory(db_session), get_settings()) is False
    assert broken.calls == [["warm up"]]  # it tried, and the error did not escape
