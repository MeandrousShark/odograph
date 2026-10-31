"""DEMO_MODE: the banner must appear on every page of the trial, must never
appear on a plain DEV_NO_AUTH site, and must never claim authentication is off
when it is not."""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import DEMO_MODE_NOTICE, Config
from app.main import make_templates

ROOT = Path(__file__).resolve().parents[1]
NOTICE = "Demo mode: synthetic data, not persistent, authentication disabled."
USER = {
    "id": 7, "name": "Sam Rivera", "email": "sam@example.com", "is_admin": True,
    "has_avatar": False, "avatar_version": 0,
}


def _render(user: dict | None, **config) -> str:
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test", **config))
    return templates.env.get_template("base.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/")),
        user=user, csrf="test-csrf", csp_nonce="test-nonce",
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/x")
    monkeypatch.setenv("SESSION_SECRET", "session-secret-long-enough-for-config")
    monkeypatch.delenv("DEV_NO_AUTH", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    return monkeypatch


def test_the_notice_is_the_agreed_sentence():
    assert DEMO_MODE_NOTICE == NOTICE


@pytest.mark.parametrize("user", [USER, None], ids=["signed-in", "public"])
def test_banner_shows_on_signed_in_and_public_pages(user):
    body = _render(user, dev_no_auth=True, demo_mode=True)

    assert f'<p class="demo-banner" role="note">{NOTICE}</p>' in body
    assert body.index("demo-banner") < body.index("<header>")


def test_banner_cannot_be_dismissed():
    body = _render(USER, dev_no_auth=True, demo_mode=True)
    paragraph = body.split('class="demo-banner"', 1)[1].split("</p>", 1)[0]

    assert "<button" not in paragraph
    # Nothing in the shell can target it to hide it.
    assert (ROOT / "app/templates/base.html").read_text().count("demo-banner") == 1


@pytest.mark.parametrize("user", [USER, None], ids=["signed-in", "public"])
def test_dev_no_auth_alone_shows_no_banner(user):
    assert "demo-banner" not in _render(user, dev_no_auth=True)


def test_startup_refuses_demo_mode_without_dev_no_auth(env):
    env.setenv("DEMO_MODE", "1")

    with pytest.raises(RuntimeError, match="DEMO_MODE=1 requires DEV_NO_AUTH=1"):
        Config.from_env()


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_a_dev_no_auth_value_other_than_1_still_refuses(env, value):
    env.setenv("DEMO_MODE", "1")
    env.setenv("DEV_NO_AUTH", value)

    with pytest.raises(RuntimeError, match="DEMO_MODE=1 requires DEV_NO_AUTH=1"):
        Config.from_env()


def test_demo_mode_logs_the_notice_at_warning(env, caplog):
    env.setenv("DEMO_MODE", "1")
    env.setenv("DEV_NO_AUTH", "1")

    with caplog.at_level(logging.INFO, logger="app.config"):
        cfg = Config.from_env()

    assert cfg.demo_mode is True
    assert [r.levelname for r in caplog.records if r.getMessage() == NOTICE] == ["WARNING"]


def test_dev_no_auth_alone_leaves_demo_mode_off_and_logs_no_notice(env, caplog):
    env.setenv("DEV_NO_AUTH", "1")

    with caplog.at_level(logging.INFO, logger="app.config"):
        cfg = Config.from_env()

    assert cfg.dev_no_auth is True
    assert cfg.demo_mode is False
    assert NOTICE not in caplog.messages


def test_real_installs_default_to_no_demo_mode(env):
    cfg = Config.from_env()

    assert cfg.demo_mode is False
    assert cfg.dev_no_auth is False


def test_banner_styles_use_design_tokens_only():
    css = (ROOT / "static/style.css").read_text()
    rule = css.split(".demo-banner {", 1)[1].split("}", 1)[0]

    assert "var(--warn)" in rule
    assert "#" not in rule
    assert "position:" not in rule
