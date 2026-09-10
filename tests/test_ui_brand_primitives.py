from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from app.main import make_templates


ROOT = Path(__file__).parents[1]
BASE = (ROOT / "app/templates/base.html").read_text()
MACRO = (ROOT / "app/templates/_icons.html").read_text()
CSS = (ROOT / "static/style.css").read_text()
FAVICON = (ROOT / "static/favicon.svg").read_text()
SPRITE = (ROOT / "static/icons.svg").read_text()


def _render_base() -> str:
    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    return templates.env.get_template("base.html").render(
        user={"name": "Tester"}, csrf_token="test", csp_nonce="nonce", static_version="v1"
    )


def _css_block(selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS)
    assert match, f"missing CSS rule {selector}"
    return match.group(1)


def test_favicon_and_brand_mark_are_local_and_accent_painted():
    rendered = _render_base()

    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg?v=v1">' in rendered
    assert '<span class="brand-mark" aria-hidden="true"></span>' in rendered
    brand = rendered.split('<a href="/" class="brand-link">', 1)[1].split("</a>", 1)[0]
    assert ">Odograph</span>" in brand
    assert 'aria-hidden="true"' in brand
    assert 'background-color: var(--accent-primary)' in CSS
    assert '-webkit-mask: url("/static/favicon.svg")' in CSS
    assert 'mask: url("/static/favicon.svg")' in CSS
    assert "#9184d9" not in FAVICON
    assert 'stroke="currentColor"' in FAVICON
    assert 'fill="currentColor"' in FAVICON
    assert ":root { color: #6c4eaa; }" in FAVICON
    assert "prefers-color-scheme: dark" in FAVICON
    assert "color: #b9a8ff" in FAVICON
    assert "@import" not in FAVICON
    root = ET.fromstring(FAVICON)
    assert root.attrib["viewBox"] == "0 0 24 24"


def test_local_icon_sprite_is_small_current_color_and_accessible_by_convention():
    root = ET.fromstring(SPRITE)
    symbols = root.findall("{http://www.w3.org/2000/svg}symbol")
    names = {symbol.attrib["id"] for symbol in symbols}
    assert names == {
        "gauge", "gear", "sign-out", "caret-left", "caret-right", "caret-down",
        "arrow-left", "arrow-right", "plus", "pencil", "dots-three", "check", "x",
        "warning", "search", "list-dashes", "check-circle", "chart-line",
        "dots-three-circle", "briefcase", "house", "lightning", "download",
    }
    assert len(symbols) <= 23
    for symbol in symbols:
        assert "currentColor" in ET.tostring(symbol, encoding="unicode")

    assert "currentColor" in MACRO
    assert 'aria-hidden="true"' in MACRO
    assert 'role="img" aria-label="{{ label }}" title="{{ label }}"' in MACRO
    assert 'href="/static/icons.svg?v={{ static_version }}#{{ name }}"' in MACRO
    assert "focusable=\"false\"" in MACRO

    templates = make_templates(SimpleNamespace(display_tz=None, app_version="test"))
    labelled = templates.env.from_string(
        '{% from "_icons.html" import icon with context %}{{ icon("gear", label="Settings", decorative=false) }}'
    ).render(static_version="v1")
    decorative = templates.env.from_string(
        '{% from "_icons.html" import icon with context %}{{ icon("gauge") }}'
    ).render(static_version="v1")
    assert 'role="img" aria-label="Settings" title="Settings"' in labelled
    assert 'aria-hidden="true"' in decorative
    assert '?v=v1#gear' in labelled


def test_shared_primitives_keep_interface_and_business_semantics_separate():
    for selector in (
        ".page-title", ".page-title-heading", ".page-title-eyebrow", ".page-title-subtitle",
        ".page-title-actions", ".notice", ".badge", ".badge-accent", ".icon-button",
        ".compact-actions", ".control-compact",
    ):
        assert selector in CSS
    for selector, token in (
        (".notice-neutral", "--border-strong"),
        (".notice-success", "--success"),
        (".notice-warning", "--warn"),
        (".notice-danger", "--danger"),
        (".badge-success", "--success"),
        (".badge-warning", "--warn"),
        (".badge-danger", "--danger"),
    ):
        block = _css_block(selector)
        assert token in block
        assert "--accent-" not in block
    assert "--cat-business" in _css_block(".category-business")
    assert "--cat-personal" in _css_block(".category-personal")
    assert "--accent-primary" in _css_block(".badge-accent")
    assert "--accent-" not in _css_block(".badge-success")
    assert "--control-height" in _css_block(".icon-button")
    assert "--control-height" in _css_block(".control-compact")
    assert "gap: var(--space-2)" in _css_block(".control")
    assert ".icon-button:focus-visible" in CSS
    assert "var(--focus-ring)" in _css_block(".icon-button:focus-visible")


def test_control_primitives_carry_the_soft_border_treatment_application_wide():
    # The soft hairline border and dropped control shadow proven on the
    # Dashboard ("View all trips", "Add manual trip", the week-nav chevrons)
    # live on the shared primitives themselves now, not a page-scoped
    # override reaching only those Dashboard elements. A future page
    # redesign copying that override pattern back in would fight these
    # primitives instead of matching them, so the override must stay gone.
    soft_border = "border: 1px solid color-mix(in srgb, var(--border) 45%, transparent);"
    for selector in (".control", ".control-secondary", ".icon-button"):
        block = _css_block(selector)
        assert "box-shadow" not in block
        assert "var(--control-shadow)" not in block
    assert soft_border in _css_block(".control-secondary")
    assert soft_border in _css_block(".icon-button")
    assert "var(--radius-md)" in _css_block(".control")
    assert "var(--radius-md)" in _css_block(".icon-button")
    pills_block = _css_block(".filter-bar .pills a")
    assert soft_border in pills_block
    assert "var(--radius-md)" in pills_block
    assert "box-shadow" not in pills_block
    assert ".dashboard-links .control-secondary, .week-nav-chevron {" not in CSS
    assert "deliberately soften off the shared .control-secondary" not in CSS


def test_control_fill_is_a_recessed_token_distinct_from_control_bg():
    # --control-fill is a new semantic token sitting alongside --control-bg,
    # not a replacement for it. --control-bg still backs the Dashboard's
    # classify pair (the color-mix pairing already reviewed and approved in
    # the browser), so it must keep resolving to --surface-elevated and stay
    # out of every primitive this task recesses.
    assert "--control-bg: var(--surface-elevated);" in CSS

    light_default_block = CSS.split(":root {\n  --bg: var(--light-bg);", 1)[1].split("\n}", 1)[0]
    assert "--control-fill: var(--surface-subtle);" in light_default_block

    dark_media_block = CSS.split("@media (prefers-color-scheme: dark) {\n  :root {", 1)[1].split(
        "\n  }\n}", 1
    )[0]
    assert "--control-fill: var(--dark-control-fill);" in dark_media_block

    light_attr_block = CSS.split(':root[data-theme="light"] {', 1)[1].split("\n}", 1)[0]
    assert "--control-fill: var(--surface-subtle);" in light_attr_block

    dark_attr_block = CSS.split(':root[data-theme="dark"] {', 1)[1].split("\n}", 1)[0]
    assert "--control-fill: var(--dark-control-fill);" in dark_attr_block

    # All four theme mapping blocks define the token, and Dark maps it to
    # --dark-control-fill, a ramp entry darker than --dark-bg itself, so a
    # control never falls back to matching the bare page (or the lightest
    # ramp entry, --surface-elevated, which is what --control-bg still is).
    assert CSS.count("--control-fill: var(--surface-subtle);") == 2
    assert CSS.count("--control-fill: var(--dark-control-fill);") == 2
    assert "--control-fill: var(--bg);" not in CSS
    assert "--control-fill: var(--surface-elevated)" not in CSS

    for selector in (
        ".control-secondary", ".icon-button", ".filter-bar .pills a",
        'button, input:not([type="checkbox"]):not([type="radio"]), select, textarea',
        'input[type="file"]::file-selector-button',
    ):
        block = _css_block(selector)
        assert "var(--control-fill)" in block
        assert "var(--control-bg)" not in block

    # The classify pair keeps consuming --control-bg untouched, so a future
    # edit that folds the two tokens together would be caught here.
    for selector in (
        ".category-segmented-input:checked + .category-segmented-personal",
        ".category-segmented-input:checked + .category-segmented-business",
    ):
        assert "var(--control-bg)" in _css_block(selector)


def test_control_fill_raised_steps_the_mobile_category_pair_off_the_card():
    # --control-fill-raised is a new semantic token alongside --control-fill.
    # A control filled with --surface-elevated sits almost flush against a
    # --surface-raised card in dark (they are one step apart), so the shared
    # mobile category pair needs a fill that reads as a step above the card
    # instead, while light stays on --surface-elevated (already white)
    # unchanged.
    light_default_block = CSS.split(":root {\n  --bg: var(--light-bg);", 1)[1].split("\n}", 1)[0]
    assert "--control-fill-raised: var(--surface-elevated);" in light_default_block

    dark_media_block = CSS.split("@media (prefers-color-scheme: dark) {\n  :root {", 1)[1].split(
        "\n  }\n}", 1
    )[0]
    assert "--control-fill-raised: var(--surface-subtle);" in dark_media_block

    light_attr_block = CSS.split(':root[data-theme="light"] {', 1)[1].split("\n}", 1)[0]
    assert "--control-fill-raised: var(--surface-elevated);" in light_attr_block

    dark_attr_block = CSS.split(':root[data-theme="dark"] {', 1)[1].split("\n}", 1)[0]
    assert "--control-fill-raised: var(--surface-subtle);" in dark_attr_block

    assert CSS.count("--control-fill-raised: var(--surface-elevated);") == 2
    assert CSS.count("--control-fill-raised: var(--surface-subtle);") == 2

    # The mobile .trip-quick-button rule is one of several rules sharing that
    # selector (the base rule and the >=761px hover-width rule also match),
    # so pull it out by its distinguishing "width: 100%" declaration rather
    # than the shared selector alone.
    mobile_rule_match = re.search(
        r"\.trip-quick-button \{ width: 100%;[^}]*\}", CSS
    )
    assert mobile_rule_match, "missing the mobile .trip-quick-button rule"
    mobile_rule = mobile_rule_match.group(0)
    assert "var(--control-fill-raised)" in mobile_rule
    assert "var(--control-bg)" not in mobile_rule


def test_brand_assets_and_templates_have_no_remote_font_or_icon_requests():
    sources = [BASE, MACRO, CSS, FAVICON, SPRITE]
    for source in sources:
        assert "fonts.googleapis.com" not in source
        assert "fonts.gstatic.com" not in source
        assert "phosphor" not in source.lower()
        urls = re.findall(r"https?://[^\s\"']+", source)
        assert urls == [] or urls == ["http://www.w3.org/2000/svg"]
    assert "/static/icons.svg" in MACRO
    assert "/static/favicon.svg" in BASE
