"""Templates must not depend on htmx features that evaluate JavaScript.

The app's CSP sets `script-src 'self' 'nonce-...'` without 'unsafe-eval'.
htmx evaluates `js:`/`javascript:` hx-vals, hx-vars, hx-on and hx-trigger
event filters with `Function`, which that CSP blocks; htmx then sends no
request at all and the control silently does nothing (B26, split trip).
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).parents[1] / "app" / "templates"
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
EVAL_PATTERNS = {
    "js: hx-vals": re.compile(r"hx-vals\s*=\s*\\?[\"']\s*(?:js|javascript):"),
    "hx-vars": re.compile(r"\bhx-vars\b"),
    "hx-on": re.compile(r"\bhx-on\b|\bhx-on[:-]"),
    "hx-trigger filter": re.compile(r"hx-trigger\s*=\s*[\"'][^\"']*\["),
}


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_templates_are_found():
    assert any(path.name == "trip.html" for path in _templates())


@pytest.mark.parametrize("path", _templates(), ids=lambda path: path.name)
def test_template_uses_no_eval_dependent_htmx_feature(path):
    source = JINJA_COMMENT.sub("", path.read_text())
    found = [name for name, pattern in EVAL_PATTERNS.items() if pattern.search(source)]
    assert not found, f"{path.name} uses {found}, which the CSP blocks"


def test_guard_catches_the_original_split_button():
    original = (
        "'<button type=\"button\" hx-post=\"' + window.location.pathname + '/split\" ' +\n"
        "'hx-vals=\\'js:{\"point_id\": window.__splitPointId}\\' ' +"
    )
    assert EVAL_PATTERNS["js: hx-vals"].search(original)
