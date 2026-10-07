from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.page import render_page
from app.account_context import AccountConnection, AccountPrincipal
from app.ui import review


@pytest.fixture(autouse=True)
def storage_reads(monkeypatch):
    reads = []

    async def status(conn):
        reads.append(conn)
        return {"total_bytes": 128, "warning": False}

    monkeypatch.setattr("app.page.storage_status", status)
    return reads


class _Cursor:
    async def fetchone(self):
        return (5,)


class _Connection:
    def __init__(self):
        self.queries = []

    async def execute(self, query, params):
        self.queries.append((query, params))
        return _Cursor()


class _ConnectionContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self):
        self.conn = _Connection()

    def connection(self):
        return _ConnectionContext(AccountConnection(self.conn, AccountPrincipal(41, True, 1)))


class _Templates:
    def __init__(self):
        self.calls = []

    def TemplateResponse(self, request, template, context, status_code=200):
        self.calls.append((request, template, context, status_code))
        return context


def test_render_page_queries_its_account_count_and_storage_once(storage_reads):
    pool = _Pool()
    templates = _Templates()
    request = SimpleNamespace(state=SimpleNamespace(account_pool=pool), app=SimpleNamespace(state=SimpleNamespace(templates=templates)))

    context = asyncio.run(render_page(request, "dashboard.html", {"user": {"id": 1}}))

    assert pool.conn.queries == [("SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified'", (41,))]
    assert context["review_count"] == 5
    assert context["storage"] == {"total_bytes": 128, "warning": False}
    assert len(storage_reads) == 1
    assert storage_reads[0].principal.account_id == 41
    assert templates.calls[0][1] == "dashboard.html"


def test_render_page_preserves_a_nondefault_status_code():
    pool = _Pool()
    templates = _Templates()
    request = SimpleNamespace(state=SimpleNamespace(account_pool=pool), app=SimpleNamespace(state=SimpleNamespace(templates=templates)))

    asyncio.run(render_page(request, "account_security.html", {}, status_code=400))

    assert templates.calls[0][3] == 400


def test_render_page_reuses_an_explicit_account_connection_without_another_borrow(storage_reads):
    raw = _Connection()
    conn = AccountConnection(raw, AccountPrincipal(41, True, 1))
    templates = _Templates()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(templates=templates)))

    context = asyncio.run(render_page(request, "review.html", {}, conn=conn))

    assert raw.queries == [("SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified'", (41,))]
    assert context["review_count"] == 5
    assert storage_reads == [conn]


def test_review_full_page_reuses_its_open_account_connection(monkeypatch):
    raw = _Connection()
    conn = AccountConnection(raw, AccountPrincipal(41, True, 1))
    templates = _Templates()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(templates=templates)))

    async def rows(conn):
        return []

    monkeypatch.setattr(review, "list_vehicles", rows)
    monkeypatch.setattr(review, "_fetch_recent_purposes", rows)
    context = asyncio.run(review._render_review_card(
        request, conn, "review.html", {"trip": None, "remaining": 0}, "", "", "",
    ))

    assert len(raw.queries) == 1
    assert raw.queries[0][1] == (41,)
    assert context["review_count"] == 5
    assert templates.calls[0][1] == "review.html"


def test_review_fragment_does_not_use_the_full_page_renderer(monkeypatch):
    templates = _Templates()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(templates=templates)))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("partial renders must not query for a shell badge")

    async def no_vehicles(conn):
        return []

    async def no_purposes(conn):
        return []

    monkeypatch.setattr(review, "render_page", fail_if_called)
    monkeypatch.setattr(review, "list_vehicles", no_vehicles)
    monkeypatch.setattr(review, "_fetch_recent_purposes", no_purposes)

    context = asyncio.run(
        review._render_review_card(
            request, object(), "_review_card.html", {"trip": None, "remaining": 0}, "", "", ""
        )
    )

    assert "review_count" not in context
