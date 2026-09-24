"""The invite fragment is a bearer secret, including same-document navigation."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.main import make_templates

pytestmark = pytest.mark.ops


def _inline_script() -> str:
    templates = make_templates(SimpleNamespace(display_tz=ZoneInfo("UTC"), app_version="test"))
    html = templates.env.get_template("invite.html").render(
        user=None, csrf="test-csrf", csp_nonce="test-nonce", display_timezone="UTC",
    )
    start = html.index("function consumeFragment()")
    start = html.rindex("<script", 0, start)
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    assert start < html.index('src="/static/vendor/htmx/htmx.min.js"')
    return html[start:end]


HARNESS = r"""
const vm = require("node:vm");
const script = process.argv[1];
const initialHash = process.argv[2];
const listeners = new Map();
const field = { value: "" };
let ready = false;
const replacements = [];
const location = { hash: initialHash };
const document = {
  getElementById(id) {
    if (id !== "invitation-token" || !ready) return null;
    return field;
  },
};
const window = {
  location,
  history: {
    replaceState(state, title, url) {
      replacements.push({ state, title, url });
      location.hash = "";
    },
  },
  addEventListener(type, callback) { listeners.set(type, callback); },
};
vm.runInNewContext(script, { window, document, URLSearchParams });
const beforeDom = { hash: location.hash, value: field.value, replacements: replacements.length };
ready = true;
listeners.get("DOMContentLoaded")();
const afterDom = { hash: location.hash, value: field.value };
location.hash = "#token=bar";
listeners.get("hashchange")();
const afterHashchange = { hash: location.hash, value: field.value };
process.stdout.write(JSON.stringify({ beforeDom, afterDom, afterHashchange, replacements }));
"""


@pytest.mark.parametrize("initial_hash,initial_value,initial_replacements", [
    ("#token=foo", "foo", 1),
    ("", "", 0),
])
def test_invite_fragment_is_scrubbed_on_load_and_same_document_navigation(
    initial_hash, initial_value, initial_replacements,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", HARNESS, _inline_script(), initial_hash],
        cwd=Path(__file__).parents[1], text=True, capture_output=True,
        check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["beforeDom"] == {
        "hash": "", "value": "", "replacements": initial_replacements,
    }
    assert state["afterDom"] == {"hash": "", "value": initial_value}
    assert state["afterHashchange"] == {"hash": "", "value": "bar"}
    assert state["replacements"] == [
        {"state": None, "title": "", "url": "/invite"}
    ] * (initial_replacements + 1)
