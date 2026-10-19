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


def test_unlimited_account_shows_usage_only():
    body = _render(_usage(account_limit_bytes=None, raw_limit_bytes=None,
                          enhancement_limit_bytes=None), "settings.html")
    assert "This account has no storage limit." in body
    assert "768 bytes used" in body and "128 bytes used" in body and "64 bytes used" in body
    assert "<progress" not in body
    assert "%)" not in body
    assert "Nearly full" not in body and "allowance</p>" not in body
    assert "ask an administrator for more space" not in body
    assert 'aria-label="Storage allowance"' not in body


def test_unlimited_sub_limit_hides_only_that_bar():
    body = _render(_usage(raw_limit_bytes=None), "settings.html")
    assert "This account has no storage limit." not in body
    assert 'aria-label="All saved data usage"' in body
    assert 'aria-label="Tracking messages usage"' not in body
    assert "128 bytes used" in body
    assert 'aria-label="Road matching and saved addresses usage"' in body
    assert "ask an administrator for more space" in body


def test_storage_recovery_points_members_at_an_administrator():
    body = _render(_usage(warning=True), "settings.html")
    assert "ask an administrator for more space" in body
    assert "ask the operator" not in body


SUB_MIB_USAGE = {
    "total_bytes": 1037968, "actual_bytes": 1033872, "reserved_bytes": 4096,
    "raw_bytes": 128 * 1024, "enhancement_bytes": 64 * 1024,
    "raw_limit_bytes": 256 * 1024, "enhancement_limit_bytes": 128 * 1024,
}


def _storage_section(body):
    return body.split('id="storage-allowance"', 1)[1].split("</section>", 1)[0]


def test_settings_never_pairs_bytes_with_mib_or_gib_for_sub_mib_usage():
    unlimited = _render(_usage(**SUB_MIB_USAGE, account_limit_bytes=None), "settings.html")
    section = _storage_section(unlimited)
    assert "1013.6 KiB used" in section
    assert "bytes" not in section

    limited = _render(_usage(**SUB_MIB_USAGE, account_limit_bytes=50 * 1024 ** 2), "settings.html")
    section = _storage_section(limited)
    assert "1.0 MiB of 50.0 MiB (2.0%)" in section
    assert "bytes" not in section


def test_settings_pairs_usage_with_its_limit_in_the_limits_unit():
    body = _render(_usage(total_bytes=512 * 1024, account_limit_bytes=768 * 1024), "settings.html")
    assert "512.0 KiB of 768.0 KiB (66.7%)" in _storage_section(body)
