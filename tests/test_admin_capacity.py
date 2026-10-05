"""Committed administrator outcomes survive a busy metadata refresh."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import admin
from app.account_context import AccountPrincipal
from app.capacity import CapacityBusy

pytestmark = pytest.mark.capacity_contract


@pytest.mark.parametrize("invitation", [False, True])
def test_committed_outcome_retains_notice_and_one_time_invitation(monkeypatch, invitation):
    async def busy(*args):
        raise CapacityBusy("identity lane full")

    monkeypatch.setattr(admin, "_load_page_data", busy)
    request = SimpleNamespace(state=SimpleNamespace(principal=AccountPrincipal(1, True, 1)))
    result = {
        "email": "person@example.invalid", "token": "one-time<&token",
        "link": "https://example.invalid/invite#token=one-time-token",
        "email_status": "The invitation email was not sent.",
    } if invitation else None
    response = asyncio.run(admin._render_accounts(
        request, {"id": 1}, notice="Action completed <once>.",
        invite_result=result, committed=True,
    ))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["referrer-policy"] == "no-referrer"
    body = response.body.decode()
    assert "Action completed &lt;once&gt;." in body
    assert "Reload account details" in body
    if invitation:
        assert "one-time&lt;&amp;token" in body
        assert result["email_status"] in body
        assert result["link"] in body


def test_precommit_metadata_pressure_still_reports_busy(monkeypatch):
    async def busy(*args):
        raise CapacityBusy("identity lane full")

    monkeypatch.setattr(admin, "_load_page_data", busy)
    request = SimpleNamespace(state=SimpleNamespace(principal=AccountPrincipal(1, True, 1)))
    with pytest.raises(CapacityBusy):
        asyncio.run(admin._render_accounts(request, {"id": 1}))
