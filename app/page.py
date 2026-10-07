"""Shared rendering for authenticated full-page templates."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request

from app.account_context import AccountConnection, account_id
from app.capacity import current_owner, owned_thread
from app.storage import storage_status


async def render_page(
    request: Request,
    template: str,
    context: Mapping,
    *,
    status_code: int = 200,
    conn: AccountConnection | None = None,
):
    """Render an authenticated shell page with its account's Review count."""
    if conn is None:
        async with request.state.account_pool.connection() as borrowed:
            review_count = await _fetch_review_count(borrowed)
            storage = await storage_status(borrowed)
    else:
        review_count = await _fetch_review_count(conn)
        storage = await storage_status(conn)
    return await render_template(request, template, {**context, "review_count": review_count,
                                                    "storage": storage},
                                 status_code=status_code)


async def _fetch_review_count(conn: AccountConnection) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified'",
        (account_id(conn),),
    )
    return (await cur.fetchone())[0]


async def render_template(request, template, context, **kwargs):
    """Keep expensive template assembly under the actual full-result operation lifetime."""
    render = lambda: request.app.state.templates.TemplateResponse(request, template, context, **kwargs)
    owner = current_owner()
    if owner is not None and owner.lane in ("navigation", "foreground"):
        return await owned_thread(render)
    return render()
