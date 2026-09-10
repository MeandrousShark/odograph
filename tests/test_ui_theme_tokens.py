from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates


ROOT = Path(__file__).parents[1]
CSS = (ROOT / "static/style.css").read_text()
BASE = (ROOT / "app/templates/base.html").read_text()
SETTINGS = (ROOT / "app/templates/settings.html").read_text()
_DEFAULT_SETTINGS_USER = {"id": 1, "name": "Tester", "is_admin": True}


def _hex(name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\b", CSS)
    assert match, f"missing color token {name}"
    return match.group(1)


def _mode_hex(mode: str, name: str) -> str:
    rule = re.compile(rf':root\[data-theme="{re.escape(mode)}"\]\s*\{{([^{{}}]*)\}}')
    for block in rule.findall(CSS):
        match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})\b", block)
        if match:
            return match.group(1)
    raise AssertionError(f"missing {mode} color token {name}")


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _render_settings(user=_DEFAULT_SETTINGS_USER) -> str:
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    return templates.env.get_template("settings.html").render(
        boundary_overrides=[], rates=[], vehicles=[], odometer=[], places=[], rules=[],
        geocode_enabled=False,
        user=user, csrf_token="test",
    )


def test_nocturne_semantic_ramps_replace_the_old_flavor_layer():
    assert "Catppuccin" not in CSS
    assert "--latte-" not in CSS
    assert "--macchiato-" not in CSS
    for token in (
        "--bg:", "--bg-alt:", "--surface:", "--surface-elevated:", "--fg:",
        "--fg-muted:", "--border:", "--border-strong:", "--accent-primary:",
        "--accent-focus:", "--accent-active:", "--accent-selected:",
        "--accent-progress:", "--accent-decorative:", "--success:", "--route:",
        "--cat-unclassified:", "--cat-nondeductible:",
    ):
        assert token in CSS


def test_each_accent_has_light_and_dark_interface_values():
    for family in ("purple", "blue", "green", "red"):
        for role in ("primary", "focus", "active", "selected", "progress", "decorative"):
            assert f"--accent-{family}-{role}:" in CSS
        assert f':root[data-accent="{family}"]' in CSS
    assert re.findall(r':root\[data-accent="([a-z]+)"\]', CSS) == [
        "purple", "blue", "green", "red",
    ]

    # Primary, focus, and boundary values remain readable in both modes.
    # Category and status tokens are separate declarations, so changing an
    # accent cannot change their values.
    for mode, ground, surface in (
        ("light", _hex("light-bg"), _hex("light-surface")),
        ("dark", _hex("dark-bg"), _hex("dark-surface")),
    ):
        for family in ("purple", "blue", "green", "red"):
            for role in ("focus", "active", "selected"):
                color = _mode_hex(mode, f"accent-{family}-{role}")
                assert _contrast(color, ground) >= 3
                assert _contrast(color, surface) >= 3
            primary = _mode_hex(mode, f"accent-{family}-primary")
            on_primary = _mode_hex(mode, f"accent-{family}-on-primary")
            assert _contrast(primary, on_primary) >= 4.5
    assert "--cat-business: var(--accent-" not in CSS
    assert "--cat-personal: var(--accent-" not in CSS
    assert "--danger: var(--accent-" not in CSS
    assert "--warn: var(--accent-" not in CSS


def test_text_and_boundary_tokens_meet_the_contrast_floor():
    for mode, ground, surface in (
        ("light", _hex("light-bg"), _hex("light-surface")),
        ("dark", _hex("dark-bg"), _hex("dark-surface")),
    ):
        assert _contrast(_hex(f"{mode}-fg"), ground) >= 4.5
        assert _contrast(_hex(f"{mode}-fg-muted"), ground) >= 4.5
        assert _contrast(_hex(f"{mode}-fg-muted"), surface) >= 4.5
        assert _contrast(_hex(f"{mode}-border"), surface) >= 3
        assert _contrast(_hex(f"{mode}-border-strong"), surface) >= 3


def test_nondeductible_category_uses_a_muted_pink_in_both_modes():
    light = _hex("light-cat-nondeductible")
    dark = _hex("dark-cat-nondeductible")
    assert light == "#a45172"
    assert dark == "#f0a8c0"
    assert _contrast(light, _hex("light-bg")) >= 4.5
    assert _contrast(dark, _hex("dark-bg")) >= 4.5
    assert light not in {"#9a5b0a", "#ffc274"}
    assert dark not in {"#9a5b0a", "#ffc274"}


def test_shared_control_target_and_reduced_motion_are_retained():
    assert "--control-height: 2.75rem" in CSS
    assert "button, input, select, summary { min-height: var(--control-height); }" in CSS
    assert ".theme-picker" in CSS and "flex-wrap: wrap" in CSS and "max-width: 100%" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "transition-duration: .01ms" in CSS
    assert "animation-duration: .01ms" in CSS
    assert "font-variant-numeric: tabular-nums" in CSS


def test_bootstrap_is_safe_allowlisted_and_precedes_the_stylesheet():
    assert BASE.index("<script nonce=") < BASE.index('<link rel="stylesheet"')
    assert "try" in BASE and "catch (error)" in BASE
    assert 'localStorage.getItem("theme")' in BASE
    assert 'localStorage.getItem("accent")' in BASE
    assert 'setAttribute("data-theme", stored)' in BASE
    assert 'setAttribute("data-accent", storedAccent)' in BASE
    assert 'storedAccent === "purple"' in BASE
    assert 'storedAccent === "blue"' in BASE
    assert 'storedAccent === "green"' in BASE
    assert 'storedAccent === "red"' in BASE
    assert 'setAttribute("data-accent", "purple")' in BASE
    assert 'removeAttribute("data-accent")' not in BASE
    assert 'setAttribute("data-theme", "' not in BASE


def test_settings_has_exact_independent_theme_and_accent_choices():
    body = _render_settings()
    theme = body.split('id="theme-picker"', 1)[1].split("</fieldset>", 1)[0]
    accent = body.split('id="accent-picker"', 1)[1].split("</fieldset>", 1)[0]
    assert re.findall(r'name="theme-choice" value="([^"]+)"', theme) == [
        "system", "light", "dark",
    ]
    assert re.findall(r'name="accent-choice" value="([^"]+)"', accent) == [
        "purple", "blue", "green", "red",
    ]
    assert 'localStorage.removeItem("theme")' in body
    assert 'localStorage.setItem("theme", choice)' in body
    assert 'localStorage.setItem("accent", choice)' in body
    assert "localStorage.removeItem(\"accent\")" not in body
    assert body.count('name="theme-choice"') == 3
    assert body.count('name="accent-choice"') == 4


def test_settings_links_to_the_renamed_account_settings_destination():
    body = _render_settings()
    account_start = body.index(
        '<section class="settings-account-section" aria-labelledby="account-heading">'
    )
    account_end = body.index("</section>", account_start)
    appearance = body.index("<h2>Appearance</h2>")
    appearance_description = body.index(
        "Follows your device's light/dark setting", appearance
    )
    theme = body.index('<fieldset class="theme-picker" id="theme-picker">', appearance)
    assert body.index(
        '<a href="/settings/account">Manage your avatar, login, and password in Account Settings</a>',
        account_start,
    ) < account_end
    assert account_start < account_end < appearance < appearance_description < theme
    assert "Account Security" not in body


def test_settings_account_section_is_conditional_for_non_admin_and_dev_no_auth_sessions():
    for user in ({"id": 2, "name": "Tester", "is_admin": False}, None):
        body = _render_settings(user)
        assert "settings-account-section" not in body
        assert "<h2 id=\"account-heading\">Account</h2>" not in body
        appearance = body.index("<h2>Appearance</h2>")
        description = body.index("Follows your device's light/dark setting", appearance)
        theme = body.index('<fieldset class="theme-picker" id="theme-picker">', appearance)
        assert appearance < description < theme


def test_settings_uses_the_secondary_page_header_and_keeps_sections_in_order():
    body = _render_settings()

    assert '<div class="settings-page">' in body
    assert '<div class="settings-page-header page-title">' in body
    assert '<p class="page-title-eyebrow">Settings</p>' in body
    labels = (
        '<section class="settings-account-section"',
        '<section class="settings-section settings-appearance">',
        '<section class="settings-section settings-rates">',
        '<section class="settings-section settings-vehicles">',
        '<section class="settings-section settings-odometer">',
        '<section class="settings-section settings-places">',
        '<section class="settings-section settings-rules">',
        '<details class="section-disclosure" id="manual-trip-edits">',
        '<section class="settings-section settings-data">',
        '<section class="settings-section settings-diagnostics">',
    )
    positions = [body.index(label) for label in labels]
    assert positions == sorted(positions)


def test_settings_import_keeps_multipart_csrf_and_mobile_file_constraint():
    body = _render_settings()

    form_start = body.index('<form class="settings-import-form"')
    form = body[form_start:body.index("</form>", form_start)]
    assert 'method="post"' in form
    assert 'action="/settings/import/data"' in form
    assert 'enctype="multipart/form-data"' in form
    assert 'name="csrf_token"' in form
    assert 'name="file" accept="application/json,.json" required' in form
    assert 'name="dry_run" value="1"' in form

    mobile_start = CSS.index(
        '@media (max-width: 760px) {\n  .settings-page-header'
    )
    review_start = CSS.index(
        '@media (max-width: 760px) {\n  .review-page-header', mobile_start
    )
    mobile = CSS[mobile_start:review_start]
    assert '.settings-page .settings-import-form input[type="file"]' in mobile
    assert "width: 100%; max-width: 760px; min-width: 0;" in mobile
    assert ".settings-page .settings-import-form .settings-file-field { flex: none; }" in mobile
    assert ".settings-page-header .page-title-subtitle { display: none; }" not in mobile


def test_production_templates_do_not_load_remote_assets():
    sources = [CSS, BASE, SETTINGS]
    for source in sources:
        assert "fonts.googleapis.com" not in source
        assert "fonts.gstatic.com" not in source
        assert "phosphor" not in source.lower()
        assert "@import" not in source
