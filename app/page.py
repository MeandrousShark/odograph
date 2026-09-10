"""Shared rendering for authenticated full-page templates."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request


async def render_page(
    request: Request,
    template: str,
    context: Mapping,
    *,
    status_code: int = 200,
):
    """Render an authenticated shell page with its global Review count."""
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM trips WHERE category = 'unclassified'"
        )
        review_count = (await cur.fetchone())[0]
    return request.app.state.templates.TemplateResponse(
        request, template, {**context, "review_count": review_count}, status_code=status_code
    )
