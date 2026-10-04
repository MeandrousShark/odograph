"""Shared rendering for authenticated full-page templates."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request

from app.account_context import account_id
from app.capacity import current_owner, owned_thread


async def render_page(
    request: Request,
    template: str,
    context: Mapping,
    *,
    status_code: int = 200,
):
    """Render an authenticated shell page with its account's Review count."""
    async with request.state.account_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM trips WHERE account_id = %s AND category = 'unclassified'",
            (account_id(conn),),
        )
        review_count = (await cur.fetchone())[0]
    return await render_template(request, template, {**context, "review_count": review_count},
                                 status_code=status_code)


async def render_template(request, template, context, **kwargs):
    """Keep expensive template assembly under the actual foreground lifetime."""
    render = lambda: request.app.state.templates.TemplateResponse(request, template, context, **kwargs)
    owner = current_owner()
    if owner is not None and owner.lane == "foreground":
        return await owned_thread(render)
    return render()
