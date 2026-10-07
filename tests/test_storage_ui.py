from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.main import make_templates


def _render(storage, template="base.html"):
    templates = make_templates(SimpleNamespace(display_tz=ZoneInfo("UTC"), app_version="test"))
    return templates.env.get_template(template).render(
        storage=storage,
        user={"id": 41, "name": "Test", "is_admin": False},
        rates=[], vehicles=[], odometer=[], places=[], rules=[], boundary_overrides=[], device_fixes=[],
        diagnostics={"app_version": "test", "git_revision": "test", "schema_version": 42, "detector_version": 2},
    )


def _usage(**changes):
    return {
        "actual_bytes": 512, "reserved_bytes": 256, "total_bytes": 768,
        "raw_bytes": 128, "enhancement_bytes": 64,
        "account_limit_bytes": 1024, "raw_limit_bytes": 256, "enhancement_limit_bytes": 128,
        "warning": False, "account_blocked": False, "raw_blocked": False,
        "enhancement_paused": False, **changes,
    }


def test_warning_links_to_recovery_without_claiming_history_was_deleted():
    body = _render(_usage(warning=True))
    assert "You're getting close to your storage limit." in body
    assert 'href="/settings#storage-allowance"' in body
    assert "Your existing trips are still available." in body
    assert "New data can't be saved" not in body


def test_optional_pause_explains_raw_fallback_and_saved_enhancements():
    body = _render(_usage(warning=True, enhancement_paused=True), "settings.html")
    assert "Road matching and address lookups are paused." in body
    assert "Trips use their recorded route and distance" in body
    assert "Saved routes and addresses stay available" in body
    assert "pending work resumes" in body


def test_capacity_paused_work_is_visible_below_warning_threshold():
    body = _render(_usage(enhancement_paused=True))
    assert 'aria-label="Storage allowance"' in body
    assert "Road matching and address lookups are paused" in body
    assert 'href="/settings#storage-allowance"' in body


def test_over_budget_display_preserves_usage_above_limit_and_recovery_options():
    body = _render(_usage(warning=True, account_blocked=True, total_bytes=1536), "settings.html")
    assert "150.0%" in body
    assert "Over allowance" in body
    assert 'max="1024" value="1024"' in body
    assert "view and export your history, sign in, recover your account" in body
    assert "After the original is deleted, another retry needs space" in body


def test_healthy_shell_has_no_warning_or_recovery_banner():
    body = _render(_usage())
    assert 'aria-label="Storage allowance"' not in body
