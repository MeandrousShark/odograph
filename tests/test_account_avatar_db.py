"""DB-backed tests for account avatar support: chunk 2 (storage and
read-only serving, every avatar planted directly with SQL) and chunk 3
(upload and remove routes, exercised through a real multipart HTTP request).

Like tests/test_vehicles_db.py, needs a real Postgres and is skipped unless
TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import asyncio
import os
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from psycopg import errors
from psycopg.rows import dict_row
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from avatar_image_fixtures import (
    JPEG_UPLOAD_BYTES, NOT_AN_IMAGE_BYTES, PNG_UPLOAD_BYTES, WEBP_UPLOAD_BYTES,
    _jpeg_with_unknown_scan_huffman_table, _webp_with_invalid_vp8_partition_length,
    _webp_with_oversized_dimensions,
)
from tests.auth_db_fixtures import auth_config, bind_auth_test_roles, seed_auth_account
from app.account_context import AccountPrincipal
from app.accounts import get_account, get_account_avatar
from app.auth import AuthRedirect, _avatar_version, make_router
from app.db import MIGRATIONS_DIR, make_pool
from app.main import make_templates
from conftest import drop_and_recreate_schema, full_schema_reset, reset_db

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL to run DB-backed tests"
)
TZ = ZoneInfo("UTC")

AVATAR_BYTES = b"\x89PNG\r\n\x1a\nnot a real png, just fixture bytes"

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


async def _insert_account(conn, *, account_id: int = 1) -> None:
    await seed_auth_account(conn, owner_id=account_id, email=f"admin{account_id}@example.com")


async def _set_avatar(conn, account_id: int, *, mime: str = "image/png") -> None:
    await conn.execute(
        "UPDATE accounts SET avatar_bytes = %s, avatar_mime = %s, avatar_updated_at = now() "
        "WHERE id = %s",
        (AVATAR_BYTES, mime, account_id),
    )


async def _set_avatar_at(
    conn, account_id: int, when: str, *, mime: str = "image/png"
) -> None:
    """Same as _set_avatar, but with an explicit avatar_updated_at instead of
    now() -- lets a test pin two writes to the same wall-clock second so it's
    deterministic rather than depending on real elapsed time.
    """
    await conn.execute(
        "UPDATE accounts SET avatar_bytes = %s, avatar_mime = %s, avatar_updated_at = %s "
        "WHERE id = %s",
        (AVATAR_BYTES, mime, when, account_id),
    )


async def _get_account(pool, account_id: int):
    async with pool.control_pool.connection() as conn:
        return await get_account(conn, account_id)


async def _get_account_avatar(pool, account_id: int):
    async with pool.control_pool.connection() as conn:
        return await get_account_avatar(conn, account_id)


def _scenario(coro_factory) -> None:
    async def run():
        pool = make_pool(TEST_DB)
        await pool.open(wait=True)
        try:
            await reset_db(pool)
            await bind_auth_test_roles(pool)
            await coro_factory(pool)
        finally:
            await pool.close()

    asyncio.run(run())


async def _migration_applies_cleanly_scenario():
    pool = make_pool(TEST_DB)
    await pool.open(wait=True)
    try:
        await drop_and_recreate_schema(pool)
        try:
            async with pool.connection() as conn:
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    if int(path.name.split("_", 1)[0]) >= 24:
                        break
                    await conn.execute(path.read_text())
                # An account that existed before this migration must come
                # through untouched, with no avatar rather than an error or
                # a half-populated row.
                await _insert_account(conn)
                await conn.execute(
                    (MIGRATIONS_DIR / "024_account_avatar.sql").read_text()
                )

            async with pool.connection() as conn:
                cur = conn.cursor(row_factory=dict_row)
                await cur.execute(
                    "SELECT avatar_bytes, avatar_mime, avatar_updated_at "
                    "FROM accounts WHERE id = 1"
                )
                row = await cur.fetchone()
            assert row == {
                "avatar_bytes": None, "avatar_mime": None, "avatar_updated_at": None,
            }
        finally:
            # The loop above bypasses the migration runner (schema_migrations
            # is never populated), so restore canonical, fully-migrated state
            # before any other test can see this one -- same reasoning as
            # test_accounts_db.py's partial-replay scenario.
            await full_schema_reset(pool)
    finally:
        await pool.close()


def test_migration_applies_cleanly_and_existing_accounts_have_no_avatar():
    asyncio.run(_migration_applies_cleanly_scenario())


def test_disallowed_avatar_mime_rejected_by_check_constraint():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "UPDATE accounts SET avatar_bytes = %s, avatar_mime = 'image/gif', "
                    "avatar_updated_at = now() WHERE id = 1",
                    (AVATAR_BYTES,),
                )

    _scenario(run)


def test_partially_populated_avatar_rejected_by_check_constraint():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "UPDATE accounts SET avatar_bytes = %s WHERE id = 1", (AVATAR_BYTES,)
                )
        async with pool.connection() as conn:
            with pytest.raises(errors.CheckViolation):
                await conn.execute(
                    "UPDATE accounts SET avatar_mime = 'image/png' WHERE id = 1"
                )

    _scenario(run)


def test_get_account_avatar_round_trips_stored_bytes():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1, mime="image/jpeg")
        avatar = await _get_account_avatar(pool, 1)
        assert avatar["avatar_bytes"] == AVATAR_BYTES
        assert avatar["avatar_mime"] == "image/jpeg"
        assert avatar["avatar_updated_at"] is not None

    _scenario(run)


def test_get_account_never_selects_avatar_bytes():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1, mime="image/webp")
        account = await _get_account(pool, 1)
        assert "avatar_bytes" not in account
        assert account["avatar_mime"] == "image/webp"
        assert account["avatar_updated_at"] is not None

    _scenario(run)


def _bare_app(
    pool,
    *,
    dev_no_auth: bool = False,
    account_avatar_max_bytes: int = 512000,
    oidc_enabled: bool = False,
) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool.runtime_pool
    app.state.control_pool = pool.control_pool
    app.state.runtime_pool = pool.runtime_pool
    app.state.dev_principal = AccountPrincipal(1, True, 1)
    app.state.make_detector_runner = lambda bound: SimpleNamespace(pool=bound)
    app.state.config = auth_config(TEST_DB,
        dev_no_auth=dev_no_auth,
        initial_admin_signup=False,
        allowed_email="",
        oidc_issuer="https://idp.example.com",
        account_avatar_max_bytes=account_avatar_max_bytes,
    )
    # Chunk 3's upload/remove routes render account_security.html through
    # _render_account on both success and failure, unlike chunk 2's
    # read-only GET /account/avatar -- a real Jinja2Templates is needed here
    # now, not just an app.state.oauth attribute to read.
    app.state.oauth = object() if oidc_enabled else None
    app.state.templates = make_templates(app.state.config)
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", same_site="lax", https_only=False
    )

    @app.exception_handler(AuthRedirect)
    async def _auth_redirect(request: Request, exc: AuthRedirect):
        return RedirectResponse("/login", status_code=303)

    # Test-only session seeding: the real /login/local flow is already
    # covered elsewhere (tests/test_accounts_db.py), so this just puts a
    # session directly into the state require_user expects, the same trick
    # tests/test_csrf_non_ascii.py uses.
    @app.get("/test/login-as/{account_id}/{auth_version}")
    async def login_as(request: Request, account_id: int, auth_version: int):
        request.session["account_id"] = account_id
        request.session["auth_version"] = auth_version
        return Response(status_code=204)

    # Same trick, for the one non-admin user shape this single-admin app can
    # produce: a legacy OIDC session that hasn't completed /account/establish
    # yet (require_user, app/auth.py). Only reachable when no account row
    # exists at all -- see _legacy_oidc_available's account_exists check.
    @app.get("/test/login-as-legacy")
    async def login_as_legacy(request: Request):
        request.session["legacy_oidc"] = {
            "issuer": "https://idp.example.com",
            "subject": "legacy-subject",
            "name": "Legacy User",
            "email": "legacy@example.com",
        }
        return Response(status_code=204)

    app.include_router(make_router())
    return app


async def _client_for(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    )


def test_avatar_route_404s_when_account_has_no_avatar():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await client.get("/test/login-as/1/1")
            response = await client.get("/account/avatar")
        assert response.status_code == 404

    _scenario(run)


def test_avatar_route_404s_for_real_dev_account_without_avatar():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
        app = _bare_app(pool, dev_no_auth=True)
        async with await _client_for(app) as client:
            response = await client.get("/account/avatar")
        assert response.status_code == 404

    _scenario(run)


def test_avatar_route_does_not_serve_bytes_without_a_session():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            # No /test/login-as call: this session carries no account_id at
            # all, so require_user must reject it before the route body ever
            # runs.
            response = await client.get("/account/avatar")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert AVATAR_BYTES not in response.content

    _scenario(run)


def test_avatar_route_serves_stored_bytes_and_revalidates_via_etag():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1, mime="image/webp")

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await client.get("/test/login-as/1/1")
            response = await client.get("/account/avatar")

            assert response.status_code == 200
            assert response.content == AVATAR_BYTES
            assert response.headers["content-type"] == "image/webp"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-disposition"] == "inline"
            cache_control = response.headers["cache-control"]
            assert cache_control == "private, no-cache"
            etag = response.headers["etag"]
            assert etag

            revalidated = await client.get(
                "/account/avatar", headers={"If-None-Match": etag}
            )
            assert revalidated.status_code == 304
            assert revalidated.content == b""
            assert revalidated.headers["etag"] == etag
            assert revalidated.headers["cache-control"] == "private, no-cache"

    _scenario(run)


def test_sub_second_avatar_writes_produce_distinct_etag_and_version():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar_at(conn, 1, "2026-01-01T00:00:00.100000+00:00")

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await client.get("/test/login-as/1/1")

            first = await client.get("/account/avatar")
            assert first.status_code == 200
            first_etag = first.headers["etag"]

            first_account = await _get_account(pool, 1)

            # Same wall-clock second as above -- only the microseconds
            # differ. Truncating to whole seconds (the original bug) would
            # make this update invisible to both the ETag and avatar_version.
            async with pool.connection() as conn:
                await _set_avatar_at(conn, 1, "2026-01-01T00:00:00.900000+00:00")

            second = await client.get("/account/avatar")
            assert second.status_code == 200
            second_etag = second.headers["etag"]

            second_account = await _get_account(pool, 1)

        # Confirm the two timestamps genuinely share a wall-clock second, or
        # this wouldn't be exercising the bug at all.
        assert first_account["avatar_updated_at"].replace(microsecond=0) == (
            second_account["avatar_updated_at"].replace(microsecond=0)
        )

        assert first_etag != second_etag
        assert _avatar_version(first_account["avatar_updated_at"]) != _avatar_version(
            second_account["avatar_updated_at"]
        )

    _scenario(run)


def test_if_none_match_honors_wildcard_and_weak_comparison():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await client.get("/test/login-as/1/1")

            baseline = await client.get("/account/avatar")
            assert baseline.status_code == 200
            etag = baseline.headers["etag"]

            # RFC 9110: If-None-Match on GET uses weak comparison, so a
            # client's "W/" prefix must still match our strong ETag.
            weak = await client.get(
                "/account/avatar", headers={"If-None-Match": f"W/{etag}"}
            )
            assert weak.status_code == 304

            # "*" means "matches whatever representation currently exists".
            wildcard = await client.get(
                "/account/avatar", headers={"If-None-Match": "*"}
            )
            assert wildcard.status_code == 304

    _scenario(run)


# ---------------------------------------------------------------------------
# Chunk 3: POST /settings/account/avatar and POST /settings/account/avatar/remove
# ---------------------------------------------------------------------------

async def _login_and_get_csrf(client: httpx.AsyncClient, *, account_id: int = 1) -> str:
    await client.get(f"/test/login-as/{account_id}/1")
    page = await client.get("/settings/account")
    return CSRF_RE.search(page.text).group(1)


def _upload(
    client: httpx.AsyncClient, csrf: str, content: bytes, *,
    filename: str = "avatar", content_type: str = "application/octet-stream",
) -> "httpx.Response":
    files = {"file": (filename, content, content_type)}
    return client.post(
        "/settings/account/avatar", data={"csrf_token": csrf}, files=files
    )


def test_upload_accepts_png_jpeg_and_webp_and_stores_the_detected_mime():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            # Deliberately mismatched filename/declared type on every one of
            # these: detection must come from the bytes, never the client's
            # say-so.
            for content, expected_mime in (
                (PNG_UPLOAD_BYTES, "image/png"),
                (JPEG_UPLOAD_BYTES, "image/jpeg"),
                (WEBP_UPLOAD_BYTES, "image/webp"),
            ):
                response = await _upload(
                    client, csrf, content,
                    filename="upload.bin", content_type="application/octet-stream",
                )
                assert response.status_code == 303
                assert response.headers["location"] == "/settings/account"

                fetched = await client.get("/account/avatar")
                assert fetched.status_code == 200
                assert fetched.content == content
                assert fetched.headers["content-type"] == expected_mime

                account = await _get_account(pool, 1)
                assert account["avatar_mime"] == expected_mime

    _scenario(run)


def test_second_upload_replaces_the_first_and_changes_avatar_version():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            first = await _upload(client, csrf, PNG_UPLOAD_BYTES)
            assert first.status_code == 303
            first_fetch = await client.get("/account/avatar")
            first_etag = first_fetch.headers["etag"]
            assert first_fetch.content == PNG_UPLOAD_BYTES

            second = await _upload(client, csrf, JPEG_UPLOAD_BYTES)
            assert second.status_code == 303
            second_fetch = await client.get("/account/avatar")
            second_etag = second_fetch.headers["etag"]
            assert second_fetch.content == JPEG_UPLOAD_BYTES
            assert second_fetch.headers["content-type"] == "image/jpeg"

            assert second_etag != first_etag

    _scenario(run)


def test_oversized_upload_rejected_with_an_honest_content_length():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool, account_avatar_max_bytes=16)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            response = await _upload(
                client, csrf, PNG_UPLOAD_BYTES, filename="avatar.png", content_type="image/png",
            )
            assert response.status_code == 413
            assert response.headers["content-type"].startswith("text/html")
            assert "Avatar exceeds the 16 bytes limit." in response.text
            assert 'action="/settings/account/avatar"' in response.text
            assert 'name="csrf_token"' in response.text
            assert 'name="file"' in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_declared_oversized_upload_renders_without_reading_the_request_body():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool, account_avatar_max_bytes=16)
        async with await _client_for(app) as client:
            await _login_and_get_csrf(client)
            consumed = False

            async def body_gen():
                nonlocal consumed
                consumed = True
                raise AssertionError("declared oversized body was read")
                yield b""

            boundary = "----avataroutercapboundary"
            request = client.build_request(
                "POST",
                "/settings/account/avatar",
                content=body_gen(),
                headers={
                    "content-type": f"multipart/form-data; boundary={boundary}",
                    "content-length": "10000",
                },
            )
            response = await client.send(request)

            assert response.status_code == 413
            assert response.headers["content-type"].startswith("text/html")
            assert "Avatar exceeds the 16 bytes limit." in response.text
            assert 'action="/settings/account/avatar"' in response.text
            assert 'name="csrf_token"' in response.text
            assert 'name="file"' in response.text
            assert consumed is False

        account = await _get_account_avatar(pool, 1)
        assert account["avatar_bytes"] == AVATAR_BYTES
        assert account["avatar_mime"] == "image/png"

    _scenario(run)


def test_unauthenticated_declared_oversized_upload_is_redirected_before_body_read():
    async def run(pool):
        app = _bare_app(pool, account_avatar_max_bytes=16)
        async with await _client_for(app) as client:
            consumed = False

            async def body_gen():
                nonlocal consumed
                consumed = True
                raise AssertionError("unauthenticated body was read")
                yield b""

            boundary = "----avatarauthcapboundary"
            request = client.build_request(
                "POST",
                "/settings/account/avatar",
                content=body_gen(),
                headers={
                    "content-type": f"multipart/form-data; boundary={boundary}",
                    "content-length": "10000",
                },
            )
            response = await client.send(request)

            assert response.status_code == 303
            assert response.headers["location"] == "/login"
            assert consumed is False

    _scenario(run)


def test_oversized_upload_with_no_content_length_falls_through_to_inner_cap():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool, account_avatar_max_bytes=16)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            boundary = "----avatarinnercapboundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
                f"{csrf}\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="avatar.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("utf-8") + PNG_UPLOAD_BYTES + f"\r\n--{boundary}--\r\n".encode("utf-8")

            async def body_gen():
                yield body

            request = client.build_request(
                "POST", "/settings/account/avatar",
                content=body_gen(),
                headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            )
            assert "content-length" not in request.headers, (
                "test is only meaningful if httpx really sent no Content-Length"
            )
            response = await client.send(request)
            assert response.status_code == 413

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_plain_non_image_upload_is_rejected():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            response = await _upload(
                client, csrf, NOT_AN_IMAGE_BYTES,
                filename="notes.txt", content_type="text/plain",
            )
            assert response.status_code == 422
            assert "Unsupported file type" in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_non_image_disguised_with_an_image_filename_and_content_type_is_still_rejected():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            # The filename and declared Content-Type both claim a PNG; the
            # bytes are plain text. Detection is magic-bytes-only (app/auth.py's
            # _detect_avatar_mime), so this must be rejected exactly like the
            # honestly-labeled non-image above -- neither the client-supplied
            # filename nor its Content-Type is ever consulted.
            response = await _upload(
                client, csrf, NOT_AN_IMAGE_BYTES,
                filename="avatar.png", content_type="image/png",
            )
            assert response.status_code == 422
            assert "Unsupported file type" in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

            # And GET /account/avatar still 404s -- nothing was stored under
            # the claimed type either.
            avatar_response = await client.get("/account/avatar")
            assert avatar_response.status_code == 404

    _scenario(run)


@pytest.mark.parametrize("content", [
    # The full format/malformation matrix is already exercised in-process by
    # tests/test_account_avatar_validation.py, which is far cheaper than a
    # pool + reset_db + login + multipart upload per case. These three exist
    # only to prove the route wiring itself: one per format, each failing a
    # different way (truncated signature, deep structural validation, an
    # invalid VP8 partition length). The declared-oversized-dimensions case
    # gets its own route-wiring test below, since that failure now produces a
    # different error message than "unsupported file type".
    b"\x89PNG\r\n\x1a\n",
    _jpeg_with_unknown_scan_huffman_table(),
    _webp_with_invalid_vp8_partition_length(),
], ids=[
    # Raw image bytes make pytest's default parametrize IDs escaped binary
    # thousands of characters long, which makes failure output and
    # --durations unreadable. Short, explicit names in payload order instead.
    "png header only", "jpeg unknown scan table", "webp invalid partition length",
])
def test_truncated_or_signature_spoofed_image_upload_is_rejected(content):
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)
            response = await _upload(
                client, csrf, content,
                filename="avatar.png", content_type="image/png",
            )
            assert response.status_code == 422
            assert "Unsupported file type" in response.text

        account = await _get_account(pool, 1)
        assert account["avatar_mime"] is None

    _scenario(run)


def test_oversized_dimension_upload_is_rejected_with_the_dimension_message():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            response = await _upload(
                client, csrf, _webp_with_oversized_dimensions(),
                filename="avatar.webp", content_type="image/webp",
            )
            assert response.status_code == 422
            assert "This image is too large" in response.text
            assert "16 megapixels" in response.text
            assert "Unsupported file type" not in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_empty_upload_is_rejected():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            response = await _upload(
                client, csrf, b"", filename="empty.png", content_type="image/png",
            )
            assert response.status_code == 422
            assert "Choose an image file" in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_missing_file_part_is_rejected():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            # No files= at all -- form.get("file") comes back None, not an
            # UploadFile, exercising the same branch as a multipart request
            # with the file part simply omitted.
            response = await client.post(
                "/settings/account/avatar", data={"csrf_token": csrf}
            )
            assert response.status_code == 422
            assert "Choose an image file" in response.text

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_upload_rejects_missing_or_wrong_csrf_token():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await _login_and_get_csrf(client)

            wrong = await _upload(
                client, "wrong-token", PNG_UPLOAD_BYTES,
                filename="avatar.png", content_type="image/png",
            )
            assert wrong.status_code == 403

            missing = await client.post(
                "/settings/account/avatar",
                files={"file": ("avatar.png", PNG_UPLOAD_BYTES, "image/png")},
            )
            assert missing.status_code == 403

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_remove_rejects_missing_or_wrong_csrf_token():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            await _login_and_get_csrf(client)

            wrong = await client.post(
                "/settings/account/avatar/remove",
                data={"csrf_token": "wrong-token"},
            )
            assert wrong.status_code == 403

            missing = await client.post("/settings/account/avatar/remove", data={})
            # A missing required Form field is rejected by FastAPI's own
            # request validation (422) before check_form_csrf ever runs --
            # still a rejection, just a step earlier than the wrong-token case.
            assert missing.status_code == 422

            account = await _get_account_avatar(pool, 1)
            assert account["avatar_bytes"] == AVATAR_BYTES

    _scenario(run)


def test_unauthenticated_request_is_rejected_on_both_routes_without_mutation():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            # No login-as call: neither route should even reach the
            # database with no session at all.
            upload_response = await client.post(
                "/settings/account/avatar",
                data={"csrf_token": "x"},
                files={"file": ("avatar.png", PNG_UPLOAD_BYTES, "image/png")},
            )
            assert upload_response.status_code == 303
            assert upload_response.headers["location"] == "/login"

            remove_response = await client.post(
                "/settings/account/avatar/remove", data={"csrf_token": "x"}
            )
            assert remove_response.status_code == 303
            assert remove_response.headers["location"] == "/login"

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None

    _scenario(run)


def test_accountless_legacy_session_is_rejected_on_both_routes_without_mutation():
    async def run(pool):
        # Accountless OIDC sessions can establish identity but cannot enter
        # personal routes, including avatar upload/removal.
        app = _bare_app(pool, oidc_enabled=True)
        async with await _client_for(app) as client:
            await client.get("/test/login-as-legacy")
            page = await client.get("/account/establish")
            csrf = CSRF_RE.search(page.text).group(1)

            upload_response = await client.post(
                "/settings/account/avatar",
                data={"csrf_token": csrf},
                files={"file": ("avatar.png", PNG_UPLOAD_BYTES, "image/png")},
            )
            assert upload_response.status_code == 303
            assert upload_response.headers["location"] == "/login"

            remove_response = await client.post(
                "/settings/account/avatar/remove", data={"csrf_token": csrf}
            )
            assert remove_response.status_code == 303
            assert remove_response.headers["location"] == "/login"

            async with pool.connection() as conn:
                exists = await (await conn.execute("SELECT count(*) FROM accounts")).fetchone()
            assert exists[0] == 0

    _scenario(run)


def test_remove_clears_the_avatar_and_is_harmless_when_none_is_set():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            first = await client.post(
                "/settings/account/avatar/remove",
                data={"csrf_token": csrf, "confirm_remove": "yes"},
            )
            assert first.status_code == 303
            assert first.headers["location"] == "/settings/account"

            account = await _get_account(pool, 1)
            assert account["avatar_mime"] is None
            assert (await _get_account_avatar(pool, 1))["avatar_bytes"] is None

            after_removal = await client.get("/account/avatar")
            assert after_removal.status_code == 404

            # Removing again with nothing left to clear must still succeed.
            second = await client.post(
                "/settings/account/avatar/remove",
                data={"csrf_token": csrf, "confirm_remove": "yes"},
            )
            assert second.status_code == 303
            assert second.headers["location"] == "/settings/account"

    _scenario(run)


def test_remove_rejects_missing_or_wrong_confirmation_without_mutation():
    async def run(pool):
        async with pool.connection() as conn:
            await _insert_account(conn)
            await _set_avatar(conn, 1)

        app = _bare_app(pool)
        async with await _client_for(app) as client:
            csrf = await _login_and_get_csrf(client)

            missing = await client.post(
                "/settings/account/avatar/remove", data={"csrf_token": csrf}
            )
            assert missing.status_code == 400

            wrong = await client.post(
                "/settings/account/avatar/remove",
                data={"csrf_token": csrf, "confirm_remove": "no"},
            )
            assert wrong.status_code == 400

            account = await _get_account_avatar(pool, 1)
            assert account["avatar_bytes"] == AVATAR_BYTES

    _scenario(run)
