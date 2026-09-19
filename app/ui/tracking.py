"""Own-account tracking setup and credential replacement."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from app.auth import check_form_csrf, require_user
from app.page import render_page
from app.tracking import (
    IssuedCredential, TrackingNotFound, convert_legacy_device, create_device,
    list_tracking, revoke_credential, rotate_credential,
)


async def _render_tracking(
    request: Request, user: dict, issued: IssuedCredential | None = None,
):
    async with request.state.account_pool.connection() as conn:
        devices, credentials = await list_tracking(conn)
    cfg = request.app.state.config
    ingest_url = cfg.app_url.rstrip("/") + "/ingest" if cfg.app_url else str(request.url_for("ingest"))
    response = await render_page(request, "tracking.html", {
        "user": user, "csrf": request.session.get("csrf", ""),
        "devices": devices, "credentials": credentials,
        "issued": issued, "ingest_url": ingest_url,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


def register(router: APIRouter) -> None:
    @router.get("/settings/tracking")
    async def tracking_page(request: Request, user: dict = Depends(require_user)):
        return await _render_tracking(request, user)

    @router.post("/settings/tracking/devices")
    async def tracking_create_device(
        request: Request, label: str = Form(...), csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        try:
            async with request.state.account_pool.connection() as conn:
                issued = await create_device(conn, label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _render_tracking(request, user, issued)

    @router.post("/settings/tracking/devices/{device_id}/convert")
    async def tracking_convert_device(
        request: Request, device_id: int, csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        try:
            async with request.state.account_pool.connection() as conn:
                issued = await convert_legacy_device(conn, device_id)
        except TrackingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render_tracking(request, user, issued)

    @router.post("/settings/tracking/credentials/{public_id}/rotate")
    async def tracking_rotate_credential(
        request: Request, public_id: str, csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        try:
            async with request.state.account_pool.connection() as conn:
                issued = await rotate_credential(conn, public_id)
        except TrackingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _render_tracking(request, user, issued)

    @router.post("/settings/tracking/credentials/{public_id}/revoke")
    async def tracking_revoke_credential(
        request: Request, public_id: str, csrf_token: str = Form(...),
        user: dict = Depends(require_user),
    ):
        check_form_csrf(request, csrf_token)
        try:
            async with request.state.account_pool.connection() as conn:
                await revoke_credential(conn, public_id)
        except TrackingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse("/settings/tracking", status_code=303)
