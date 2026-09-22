"""The two portable HTTP routes: GET /settings/export/data and POST
/settings/import/data. Wires format.py/export.py/normalize.py/importer.py
together into request handlers; owns request/response shaping (upload
size caps, dry-run rollback, JSON error bodies), not the bundle shaping or
validation those modules already do.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg import Rollback
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse, Response

from app.auth import check_form_csrf, require_user
from app.db import _fetch_schema_version
from app.portable.export import (
    _fetch_export_expenses,
    _fetch_export_mileage_rates,
    _fetch_export_odometer_readings,
    _fetch_export_places,
    _fetch_export_settings,
    _fetch_export_tag_rules,
    _fetch_export_trips,
    _fetch_export_vehicles,
    build_export_bundle,
)
from app.portable.importer import PortableImportError, _apply_import
from app.portable.normalize import normalize_bundle
from app.uploads import read_capped_upload

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _reject_oversized_import_upload(request: Request) -> None:
    """A route dependency for POST /settings/import/data -- runs before the
    route body ever calls request.form(), which is the only way to refuse an
    oversized upload before Starlette's multipart parser spools it to disk.
    Confirmed against this project's installed Starlette (1.3.1):
    MultiPartParser.on_part_data enforces max_part_size only for a non-file
    part; a file part is appended to its SpooledTemporaryFile with no cap of
    its own. This only works because the route below takes no File()/Form()
    parameters of its own -- declaring one there would make FastAPI call
    request.form() itself while resolving the route's parameters, which (also
    confirmed empirically against this project's installed FastAPI) happens
    before any dependency, including this one, runs.

    Content-Length covers the whole multipart envelope (boundary lines and
    part headers, not just the file bytes), so this is a conservative
    approximation of the configured limit rather than an exact one -- the
    right direction to be wrong in for a guard. It's also absent entirely
    under chunked transfer-encoding; that case falls through here to let
    read_capped_upload (app/uploads.py) catch it below, on whatever Starlette
    already spooled by the time the route body runs.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return
    cfg = request.app.state.config
    if declared_bytes > cfg.portable_import_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds the {cfg.portable_import_max_bytes}-byte limit",
        )


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/settings/export/data")
    async def export_data(request: Request, user: dict = Depends(require_user)):
        pool = request.state.account_pool
        async with pool.connection() as conn:
            bundle = build_export_bundle(
                vehicles=await _fetch_export_vehicles(conn),
                places=await _fetch_export_places(conn),
                tag_rules=await _fetch_export_tag_rules(conn),
                mileage_rates=await _fetch_export_mileage_rates(conn),
                trips=await _fetch_export_trips(conn),
                expenses=await _fetch_export_expenses(conn),
                odometer_readings=await _fetch_export_odometer_readings(conn),
                settings=await _fetch_export_settings(conn),
                schema_version=await _fetch_schema_version(conn),
                exported_at=datetime.now(timezone.utc),
            )

        def _serialize_bundle() -> bytes:
            # allow_nan=False: json.dumps otherwise writes a bare NaN/Infinity
            # token for any non-finite value already in the ledger, which
            # Python's own json.loads accepts back but isn't valid JSON per
            # RFC 8259 -- JS JSON.parse, jq, and Go's encoding/json all
            # reject it, making the file unreadable by anything but this app.
            return json.dumps(bundle, indent=2, allow_nan=False).encode("utf-8")

        try:
            # Serializing the whole database is CPU-bound and can be large;
            # offload so it doesn't block the event loop for other requests.
            content = await asyncio.to_thread(_serialize_bundle)
        except ValueError:
            log.exception("portable export: bundle contains a non-finite value")
            return JSONResponse(
                {
                    "ok": False, "error": "non_finite_value",
                    "detail": "Export failed because the ledger contains a non-finite "
                    "number (NaN or Infinity); the export was not produced.",
                },
                status_code=500,
            )
        filename = f"odograph-export-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        return Response(
            content=content, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post(
        "/settings/import/data",
        dependencies=[Depends(_reject_oversized_import_upload)],
    )
    async def import_data(request: Request, user: dict = Depends(require_user)):
        # file/dry_run/csrf_token are read from the parsed form by hand, not
        # declared as File()/Form() parameters on this function -- see
        # _reject_oversized_import_upload's docstring for why that's load-
        # bearing rather than a style choice.
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=422, detail="file is required")
        raw_dry_run = form.get("dry_run", "")
        raw_csrf_token = form.get("csrf_token", "")
        dry_run = raw_dry_run if isinstance(raw_dry_run, str) else ""
        csrf_token = raw_csrf_token if isinstance(raw_csrf_token, str) else ""

        check_form_csrf(request, csrf_token)

        cfg = request.app.state.config
        raw = await read_capped_upload(file, cfg.portable_import_max_bytes)
        if raw is None:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {cfg.portable_import_max_bytes}-byte limit",
            )

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_json", "detail": "Uploaded file is not valid JSON"},
                status_code=400,
            )

        normalized, issues = normalize_bundle(payload)
        if issues:
            return JSONResponse(
                {"ok": False, "error": "malformed_bundle", "issues": issues}, status_code=400
            )

        is_dry_run = dry_run == "1"
        pool = request.state.account_pool
        try:
            async with pool.connection() as conn:
                async with conn.transaction():
                    summary = await _apply_import(conn, normalized)
                    if is_dry_run:
                        # Runs the identical validate-then-mutate path so a
                        # dry run genuinely exercises conflict detection,
                        # then discards the mutation instead of committing it.
                        raise Rollback()
        except PortableImportError as exc:
            return JSONResponse(exc.to_response(), status_code=exc.status_code)

        return JSONResponse({"ok": True, "dry_run": is_dry_run, "counts": summary})

    return router
