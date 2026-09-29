"""Administrator account metadata, invitations, and verified recovery."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import dict_row
from starlette.responses import Response

from app.account_context import control_connection
from app.account_lifecycle import (
    AccountLifecycleUnavailable,
    list_account_security_audit,
    set_account_enabled,
    request_account_deletion, cancel_account_deletion, purge_account,
)
from app.accounts import get_account, normalize_email, safe_delivery_email
from app.account_work import external_account_work
from app.auth import (
    check_form_csrf, require_admin, _verified_account, _AccountActionRejected, _AuthSaturated,
)
from app.config import security_link_base
from app.invitations import (
    InvitationUnavailable,
    invitation_mail_admission,
    issue_invitation_record,
    list_invitations,
    resend_invitation_record,
    revoke_invitation,
)
from app.mailer import Mailer

log = logging.getLogger(__name__)
MAX_ADMIN_FORM_BYTES = 4096


async def _read_form(
    request: Request,
    fields: set[str],
    *,
    optional_fields: set[str] | None = None,
) -> dict[str, str]:
    """Read a small urlencoded form only after the route's auth dependency."""
    from app.ingest import _read_capped_body

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=400, detail="Invalid form")
    body = await _read_capped_body(request, MAX_ADMIN_FORM_BYTES)
    if body is None:
        raise HTTPException(status_code=413, detail="Invalid form")
    try:
        parsed = parse_qs(
            body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
            max_num_fields=len(fields | (optional_fields or set())),
        )
        allowed = fields | (optional_fields or set())
        if not fields <= set(parsed) or not set(parsed) <= allowed or any(
            len(values) != 1 for values in parsed.values()
        ):
            raise ValueError("invalid fields")
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid form") from None
    return {name: values[0] for name, values in parsed.items()}


def _actor(user: dict, request: Request) -> dict:
    """Add only the auth version verified by require_admin for this request."""
    principal = request.state.principal
    return {**user, "auth_version": principal.auth_version}


async def _metadata_accounts(conn) -> list[dict]:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT a.id, a.email, a.email_verified_at IS NOT NULL AS email_verified, "
        "a.is_admin, a.is_enabled, "
        "(a.password_hash IS NOT NULL AND a.password_hash <> '') AS has_password, "
        "EXISTS (SELECT 1 FROM public.oidc_identities i WHERE i.account_id = a.id) AS has_oidc, "
        "a.deletion_deadline "
        "FROM public.accounts a ORDER BY a.id"
    )
    return await cur.fetchall()


async def _multiuser_available(conn) -> bool:
    cur = await conn.execute(
        "SELECT to_regclass('public.accounts_singleton_idx') IS NULL, "
        "NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.accounts'::regclass "
        "AND conname = 'accounts_is_admin_check')"
    )
    return all(await cur.fetchone())


async def _load_page_data(
    request: Request, actor: dict,
) -> tuple[list[dict], list[dict], list[dict], bool]:
    async with control_connection(request.app.state.control_pool) as conn:
        accounts = await _metadata_accounts(conn)
        invitations = await list_invitations(conn, actor)
        audit_events = await list_account_security_audit(conn, actor)
        multiuser_available = await _multiuser_available(conn)
    checked_at = datetime.now(timezone.utc)
    for account in accounts:
        account["purge_available"] = bool(account["deletion_deadline"] and account["deletion_deadline"] <= checked_at)
    for invitation in invitations:
        if invitation["consumed_at"] is not None:
            invitation["status"] = "Accepted"
        elif invitation["revoked_at"] is not None:
            invitation["status"] = "Revoked"
        elif invitation["expires_at"] <= checked_at:
            invitation["status"] = "Expired"
        else:
            invitation["status"] = "Open"
    return accounts, invitations, audit_events, multiuser_available


async def _render_accounts(
    request: Request,
    user: dict,
    *,
    notice: str = "",
    invite_result: dict | None = None,
    status_code: int = 200,
) -> Response:
    actor = _actor(user, request)
    accounts, invitations, audit_events, multiuser_available = await _load_page_data(
        request, actor,
    )
    response = request.app.state.templates.TemplateResponse(
        request,
        "admin_accounts.html",
        {
            "user": user,
            "csrf": request.session.get("csrf", ""),
            "review_count": 0,
            "accounts": accounts,
            "actor_account": next((account for account in accounts if account["id"] == actor["id"]), None),
            "invitations": invitations,
            "audit_events": audit_events,
            "multiuser_available": multiuser_available,
            "notice": notice,
            "invite_result": invite_result,
            "oidc_purge_available": bool(getattr(request.app.state.config, "oidc_issuer", "") and getattr(request.app.state, "oauth", None)),
        },
        status_code=status_code,
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store, private"
    return response


def _invitation_message(mailer: Mailer, link: str, token: str):
    return mailer.compose(
        "Invitation to Odograph",
        "An administrator invited you to join Odograph.\n\n"
        f"Open this link to accept the invitation:\n{link}\n\n"
        f"If the link does not fill the form, enter this one-time token manually:\n{token}\n\n"
        "The invitation expires in 48 hours and can be used once.",
    )


async def _send_invitation_email(
    request: Request,
    actor: dict,
    invitation_id: int,
    email: str,
    token: str,
) -> str:
    cfg = request.app.state.config
    link_base = security_link_base(getattr(cfg, "app_url", ""))
    if not (getattr(cfg, "smtp_host", "") and getattr(cfg, "email_from", "") and link_base):
        return "not_sent"
    mailer = Mailer(
        cfg.smtp_host, cfg.smtp_port, cfg.smtp_username, cfg.smtp_password,
        cfg.smtp_security, cfg.smtp_tls_insecure, cfg.email_from, email,
    )
    link = f"{link_base}/invite#token={quote(token, safe='')}"
    message = _invitation_message(mailer, link, token)
    admission = request.app.state.security_mail
    @asynccontextmanager
    async def admit():
        async with control_connection(request.app.state.control_pool) as conn:
            async with invitation_mail_admission(conn, actor, invitation_id) as target_email:
                yield bool(target_email and target_email == email)

    try:
        admitted = await admission.send(
            mailer, message, wait=False, admit=admit,
            lease=lambda: external_account_work(request.app.state.control_pool, actor["id"]),
        )
        return "sent" if admitted else "not_sent"
    except Exception as exc:
        # SMTP exceptions can include transport details. Do not log the
        # token, message, recipient or exception text.
        log.warning("administrator invitation delivery failed (%s)", type(exc).__name__)
        return "unknown"


async def _checked_invitation_result(
    request: Request,
    user: dict,
    *,
    invitation_id: int,
    email: str,
    token: str,
    send_email: bool,
) -> Response:
    mail_status = await _send_invitation_email(
        request, _actor(user, request), invitation_id, email, token
    ) if send_email else "not_requested"
    link_base = security_link_base(getattr(request.app.state.config, "app_url", ""))
    invite_result = {
        "email": email,
        "token": token,
        "link": f"{link_base}/invite#token={quote(token, safe='')}" if link_base else None,
        "email_status": {
            "sent": "The configured SMTP server accepted the invitation email.",
            "not_sent": "The invitation email was not sent. Check that this invitation is still Open before sharing the link or token below.",
            "unknown": "Email delivery could not be confirmed. Check that this invitation is still Open before sharing the link or token below.",
            "not_requested": "Email was not requested. Copy the link or token below.",
        }[mail_status],
    }
    return await _render_accounts(
        request, user, notice="Invitation issued. The token is shown once below.",
        invite_result=invite_result,
    )


async def _set_account_enabled_route(
    request: Request, user: dict, account_id: int, *, enable: bool,
) -> Response:
    form = await _read_form(request, {"csrf_token"})
    check_form_csrf(request, form["csrf_token"])
    if account_id < 1:
        raise HTTPException(status_code=404)
    actor = _actor(user, request)
    if account_id == actor["id"]:
        return await _render_accounts(
            request, user,
            notice="Administrators cannot change their own account access here.",
            status_code=409,
        )
    async with control_connection(request.app.state.control_pool) as conn:
        multiuser_available = await _multiuser_available(conn)
    if not multiuser_available:
        return await _render_accounts(
            request, user,
            notice="Account access controls are unavailable until multi-account mode is activated.",
            status_code=409,
        )
    try:
        async with control_connection(request.app.state.control_pool) as conn:
            outcome = await set_account_enabled(conn, actor, account_id, enable=enable)
    except AccountLifecycleUnavailable:
        return await _render_accounts(
            request, user,
            notice="Account access could not be changed. Check the account and administrator requirements.",
            status_code=409,
        )
    notice = {
        "disabled": "Account disabled. Existing sessions and tracking credentials were revoked.",
        "enabled": "Account enabled. New sign-in is available; tracking credentials need reissue.",
        "already_disabled": "Account was already disabled. Access is unchanged.",
        "already_enabled": "Account was already enabled. Access is unchanged.",
    }[outcome]
    return await _render_accounts(request, user, notice=notice)


async def _deletion_route(request: Request, user: dict, account_id: int, action: str) -> Response:
    required = {"csrf_token"} if action == "cancel" else {"csrf_token", "target_email"}
    optional = {"acknowledge"} if action == "request" else {"confirm_purge", "current_password"} if action == "purge" else set()
    form = await _read_form(request, required, optional_fields=optional)
    check_form_csrf(request, form["csrf_token"])
    if account_id < 1:
        raise HTTPException(status_code=404)
    actor = _actor(user, request)
    async with control_connection(request.app.state.control_pool) as conn:
        available = await _multiuser_available(conn)
    if not available or actor["id"] == account_id:
        return await _render_accounts(request, user, notice="Deletion requires activated multi-account mode and another target account.", status_code=409)
    if action == "request" and form.get("acknowledge") != "1":
        return await _render_accounts(request, user, notice="Acknowledge the export and backup effects before starting deletion.", status_code=400)
    if action == "purge" and form.get("confirm_purge") != "1":
        return await _render_accounts(request, user, notice="Confirm permanent deletion before purging the account.", status_code=400)
    password_hash = None
    proof_nonce = None
    if action == "purge":
        async with control_connection(request.app.state.control_pool) as conn:
            current = await get_account(conn, actor["id"])
        if current and current["password_hash"] is not None and form.get("current_password"):
            try:
                verified = await request.app.state.login_limiter.run_bounded(lambda: _verified_account(
                    request, user, form.get("current_password", ""), generic_error="Fresh administrator authentication is required.",
                ))
                password_hash = verified["password_hash"]
            except _AccountActionRejected as rejected:
                return await _render_accounts(request, user, notice=rejected.error, status_code=rejected.status_code)
            except _AuthSaturated:
                return await _render_accounts(request, user, notice="Authentication is busy. Try again later.", status_code=503)
        else:
            proof_nonce = request.session.get("oidc_action_proof_nonce")
    try:
        async with control_connection(request.app.state.control_pool) as conn:
            if action == "request":
                await request_account_deletion(conn, actor, account_id, email=form["target_email"], acknowledge=True)
            elif action == "cancel":
                await cancel_account_deletion(conn, actor, account_id)
            else:
                await purge_account(conn, actor, account_id, email=form["target_email"], confirm=True,
                                    verified_password_hash=password_hash, browser_nonce=proof_nonce)
    except AccountLifecycleUnavailable:
        return await _render_accounts(request, user, notice="Deletion could not be changed. Check the grace deadline, confirmation, fresh authentication and active work.", status_code=409)
    if action == "purge" and proof_nonce is not None:
        request.session.pop("oidc_action_proof_nonce", None)
    notices = {
        "request": "Deletion scheduled. Account access is disabled for the 30-day recovery window.",
        "cancel": "Deletion cancelled. New sign-in is available; revoked sessions and tracking credentials stay revoked.",
        "purge": "Account permanently removed from the live database. Historical backups may still retain its data.",
    }
    return await _render_accounts(request, user, notice=notices[action])


def make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/admin/accounts")
    async def admin_accounts(request: Request, user: dict = Depends(require_admin)):
        return await _render_accounts(request, user)

    @router.post("/admin/accounts/{account_id}/disable")
    async def disable_account_route(
        request: Request, account_id: int, user: dict = Depends(require_admin),
    ):
        return await _set_account_enabled_route(request, user, account_id, enable=False)

    @router.post("/admin/accounts/{account_id}/enable")
    async def enable_account_route(
        request: Request, account_id: int, user: dict = Depends(require_admin),
    ):
        return await _set_account_enabled_route(request, user, account_id, enable=True)

    @router.post("/admin/accounts/{account_id}/deletion")
    async def request_deletion_route(request: Request, account_id: int, user: dict = Depends(require_admin)):
        return await _deletion_route(request, user, account_id, "request")

    @router.post("/admin/accounts/{account_id}/deletion/cancel")
    async def cancel_deletion_route(request: Request, account_id: int, user: dict = Depends(require_admin)):
        return await _deletion_route(request, user, account_id, "cancel")

    @router.post("/admin/accounts/{account_id}/purge")
    async def purge_account_route(request: Request, account_id: int, user: dict = Depends(require_admin)):
        return await _deletion_route(request, user, account_id, "purge")

    @router.post("/admin/invitations")
    async def issue_member_invitation_route(
        request: Request, user: dict = Depends(require_admin),
    ):
        form = await _read_form(
            request, {"csrf_token", "email"}, optional_fields={"send_email"},
        )
        check_form_csrf(request, form["csrf_token"])
        if "send_email" in form and form["send_email"] != "1":
            raise HTTPException(status_code=400, detail="Invalid form")
        email = normalize_email(form["email"])
        if not safe_delivery_email(email):
            return await _render_accounts(
                request, user, notice="Enter one valid email address.", status_code=400,
            )
        actor = _actor(user, request)
        async with control_connection(request.app.state.control_pool) as conn:
            multiuser_available = await _multiuser_available(conn)
        if not multiuser_available:
            return await _render_accounts(
                request, user,
                notice="Invitations are unavailable until multi-account mode is activated.",
                status_code=409,
            )
        try:
            async with control_connection(request.app.state.control_pool) as conn:
                invitation_id, token = await issue_invitation_record(conn, actor, email)
        except InvitationUnavailable:
            return await _render_accounts(
                request, user,
                notice="Could not issue the invitation. The address may already be in use or the invitation limit may have been reached.",
                status_code=400,
            )
        return await _checked_invitation_result(
            request, user, invitation_id=invitation_id, email=email, token=token,
            send_email=form.get("send_email") == "1",
        )

    @router.post("/admin/invitations/{invitation_id}/revoke")
    async def revoke_member_invitation_route(
        request: Request, invitation_id: int, user: dict = Depends(require_admin),
    ):
        form = await _read_form(request, {"csrf_token"})
        check_form_csrf(request, form["csrf_token"])
        if invitation_id < 1:
            raise HTTPException(status_code=404)
        actor = _actor(user, request)
        try:
            async with control_connection(request.app.state.control_pool) as conn:
                await revoke_invitation(conn, actor, invitation_id)
        except InvitationUnavailable:
            return await _render_accounts(
                request, user, notice="The invitation could not be revoked.", status_code=400,
            )
        return await _render_accounts(request, user, notice="Invitation revoked if it was still open.")

    @router.post("/admin/invitations/{invitation_id}/resend")
    async def resend_member_invitation_route(
        request: Request, invitation_id: int, user: dict = Depends(require_admin),
    ):
        form = await _read_form(request, {"csrf_token"}, optional_fields={"send_email"})
        check_form_csrf(request, form["csrf_token"])
        if "send_email" in form and form["send_email"] != "1":
            raise HTTPException(status_code=400, detail="Invalid form")
        if invitation_id < 1:
            raise HTTPException(status_code=404)
        actor = _actor(user, request)
        async with control_connection(request.app.state.control_pool) as conn:
            multiuser_available = await _multiuser_available(conn)
        if not multiuser_available:
            return await _render_accounts(
                request, user,
                notice="Invitations are unavailable until multi-account mode is activated.",
                status_code=409,
            )
        try:
            async with control_connection(request.app.state.control_pool) as conn:
                new_id, email, token = await resend_invitation_record(
                    conn, actor, invitation_id,
                )
        except InvitationUnavailable:
            return await _render_accounts(
                request, user,
                notice="Could not resend the invitation. It may no longer be open or its limit may have been reached.",
                status_code=400,
            )
        return await _checked_invitation_result(
            request, user, invitation_id=new_id, email=email, token=token,
            send_email=form.get("send_email") == "1",
        )

    @router.post("/admin/accounts/{account_id}/recovery")
    async def admin_account_recovery_route(
        request: Request, account_id: int, user: dict = Depends(require_admin),
    ):
        form = await _read_form(request, {"csrf_token"})
        check_form_csrf(request, form["csrf_token"])
        if account_id < 1:
            raise HTTPException(status_code=404)
        async with control_connection(request.app.state.control_pool) as conn:
            cur = await conn.execute(
                "SELECT email_verified_at IS NOT NULL, is_enabled "
                "FROM public.accounts WHERE id = %s",
                (account_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        if not row[0]:
            return await _render_accounts(
                request, user,
                notice="This account has no verified login address. Use the trusted host-local recovery procedure.",
            )
        if not row[1]:
            return await _render_accounts(
                request, user, notice="This account is disabled and cannot use password recovery.",
            )
        queue = getattr(request.app.state, "password_reset_queue", None)
        actor = _actor(user, request)
        accepted = bool(queue and queue.submit_admin(actor["id"], actor["auth_version"], account_id))
        notice = (
            "Password recovery was queued. Delivery is not confirmed."
            if accepted else
            "Recovery was not queued. Try again later."
        )
        return await _render_accounts(request, user, notice=notice)

    return router
