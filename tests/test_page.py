from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.page import render_page
from app.ui import review


class _Cursor:
    async def fetchone(self):
        return (5,)


class _Connection:
    def __init__(self):
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
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
        return _ConnectionContext(self.conn)


class _Templates:
    def __init__(self):
        self.calls = []

    def TemplateResponse(self, request, template, context, status_code=200):
        self.calls.append((request, template, context, status_code))
        return context


def test_render_page_queries_the_exact_global_unclassified_count_once():
    pool = _Pool()
    templates = _Templates()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool, templates=templates)))

    context = asyncio.run(render_page(request, "dashboard.html", {"user": {"id": 1}}))

    assert pool.conn.queries == ["SELECT count(*) FROM trips WHERE category = 'unclassified'"]
    assert context["review_count"] == 5
    assert templates.calls[0][1] == "dashboard.html"


def test_render_page_preserves_a_nondefault_status_code():
    pool = _Pool()
    templates = _Templates()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool, templates=templates)))

    asyncio.run(render_page(request, "account_security.html", {}, status_code=400))

    assert templates.calls[0][3] == 400


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
