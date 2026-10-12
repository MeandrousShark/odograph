"""Invite and reset pages carry a bearer token in the fragment, which a
server-side error page cannot restore. Their inline script blocks a password
mismatch before submit, and the error page says how to get the token back."""
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

PAGES = {
    "invite.html": {
        "path": "/invite", "token": "invitation-token", "password": "invite-password",
        "confirm": "invite-password-confirm",
        "hint": "If the invitation token field is empty, open your invitation link again or paste the token.",
    },
    "reset_password.html": {
        "path": "/reset-password", "token": "reset-token", "password": "reset-password",
        "confirm": "reset-password-confirm",
        "hint": "If the reset code field is empty, open the link in your reset email again or paste the code.",
    },
}


def _render(name: str, **context) -> str:
    templates = make_templates(SimpleNamespace(display_tz=ZoneInfo("UTC"), app_version="test"))
    return templates.env.get_template(name).render(
        user=None, csrf="test-csrf", csp_nonce="test-nonce", display_timezone="UTC", **context,
    )


def _inline_script(name: str) -> str:
    html = _render(name)
    start = html.index("function consumeFragment()")
    start = html.rindex('<script nonce="test-nonce">', 0, start)
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    assert start < html.index('src="/static/vendor/htmx/htmx.min.js"')
    return html[start:end]


HARNESS = r"""
const vm = require("node:vm");
const [script, initialHash, idsJson, stepsJson] = process.argv.slice(1);
const ids = JSON.parse(idsJson);
const listeners = new Map();
const replacements = [];
const location = { hash: initialHash };
let ready = false;
function element() {
  const handlers = new Map();
  return {
    value: "", message: "", reported: 0, handlers,
    addEventListener(type, callback) {
      if (!handlers.has(type)) handlers.set(type, []);
      handlers.get(type).push(callback);
    },
    fire(type, event) { for (const callback of handlers.get(type) || []) callback(event); },
    setCustomValidity(message) { this.message = message; },
    checkValidity() { return this.message === ""; },
    reportValidity() { this.reported += 1; return this.message === ""; },
  };
}
const form = element();
const button = element();
form.querySelector = (selector) => (selector === 'button[type="submit"]' ? button : null);
const fields = { token: element(), password: element(), confirm: element() };
fields.password.form = form;
fields.confirm.form = form;
const byId = { [ids.token]: fields.token, [ids.password]: fields.password, [ids.confirm]: fields.confirm };
const document = { getElementById(id) { return ready ? byId[id] || null : null; } };
const window = {
  location,
  history: { replaceState(state, title, url) { replacements.push(url); location.hash = ""; } },
  addEventListener(type, callback) { listeners.set(type, callback); },
};
vm.runInNewContext(script, { window, document, URLSearchParams });
ready = true;
listeners.get("DOMContentLoaded")();
const results = [];
for (const step of JSON.parse(stepsJson)) {
  if (step.set) Object.assign(fields[step.set], { value: step.value });
  if (step.input) fields[step.input].fire("input", {});
  if (step.change) fields[step.change].fire("change", {});
  if (step.click) {
    // A browser sends the submit button's click first, then runs native
    // constraint validation, and fires submit only if every field is valid.
    button.fire("click", {});
    const blocked = !fields.confirm.checkValidity();
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    if (!blocked) form.fire("submit", event);
    results.push({ blocked, submitted: !blocked && !event.prevented, message: fields.confirm.message });
  }
  if (step.submit) {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    form.fire("submit", event);
    results.push({ prevented: event.prevented, message: fields.confirm.message, reported: fields.confirm.reported });
  }
}
const afterLoad = fields.token.value;
location.hash = "#token=navigated-secret";
listeners.get("hashchange")();
process.stdout.write(JSON.stringify({
  token: afterLoad, navigated: fields.token.value, hash: location.hash, replacements, results,
  message: fields.confirm.message,
}));
"""


def _run(name: str, steps: list[dict], initial_hash: str = "") -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = PAGES[name]
    ids = {"token": page["token"], "password": page["password"], "confirm": page["confirm"]}
    result = subprocess.run(
        [node, "-e", HARNESS, _inline_script(name), initial_hash, json.dumps(ids), json.dumps(steps)],
        cwd=Path(__file__).parents[1], text=True, capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", PAGES)
def test_mismatched_confirmation_blocks_submit(name):
    state = _run(name, [
        {"set": "password", "value": "first password"},
        {"set": "confirm", "value": "other password"},
        {"input": "confirm"},
        {"submit": True},
    ])
    assert state["results"] == [{"prevented": True, "message": "Passwords do not match.", "reported": 1}]


@pytest.mark.parametrize("name", PAGES)
def test_mismatch_is_caught_on_submit_without_input_events(name):
    # Autofill or a password manager may not fire input events.
    state = _run(name, [
        {"set": "password", "value": "first password"},
        {"set": "confirm", "value": "other password"},
        {"submit": True},
    ])
    assert state["results"] == [{"prevented": True, "message": "Passwords do not match.", "reported": 1}]


@pytest.mark.parametrize("name", PAGES)
def test_matching_confirmation_allows_submit_after_a_correction(name):
    state = _run(name, [
        {"set": "password", "value": "first password"},
        {"set": "confirm", "value": "first passwor"},
        {"input": "confirm"},
        {"set": "password", "value": "first passwor"},
        {"input": "password"},
        {"submit": True},
    ])
    assert state["results"] == [{"prevented": False, "message": "", "reported": 0}]


@pytest.mark.parametrize("name", PAGES)
def test_fragment_token_still_fills_the_field_and_is_scrubbed(name):
    state = _run(name, [{"set": "password", "value": "same"}, {"set": "confirm", "value": "same"},
                        {"submit": True}], initial_hash="#token=fragment-secret")
    assert state["token"] == "fragment-secret"
    assert state["navigated"] == "navigated-secret"
    assert state["hash"] == ""
    assert state["replacements"] == [PAGES[name]["path"]] * 2
    assert state["results"] == [{"prevented": False, "message": "", "reported": 0}]


@pytest.mark.parametrize("name", PAGES)
def test_error_page_explains_how_to_restore_the_token(name):
    page = PAGES[name]
    html = _render(name, error="Passwords do not match.")
    assert page["hint"] in html
    assert f'id="{page["password"]}"' in html and f'id="{page["confirm"]}"' in html
    assert page["hint"] not in _render(name, error=None)
    # No inline handlers: CSP allows only the nonce'd script.
    assert "onsubmit" not in html and "oninput" not in html and "hx-on" not in html


@pytest.mark.parametrize("name", PAGES)
def test_autofill_without_input_events_clears_a_stale_mismatch_on_click(name):
    state = _run(name, [
        {"set": "password", "value": "first password"},
        {"set": "confirm", "value": "other password"},
        {"input": "confirm"},
        # A password manager fills both fields without input events.
        {"set": "password", "value": "filled password"},
        {"set": "confirm", "value": "filled password"},
        {"click": True},
    ])
    assert state["results"] == [{"blocked": False, "submitted": True, "message": ""}]


@pytest.mark.parametrize("name", PAGES)
def test_change_event_rechecks_and_click_still_blocks_a_mismatch(name):
    state = _run(name, [
        {"set": "password", "value": "first password"},
        {"set": "confirm", "value": "first password"},
        {"change": "confirm"},
        {"set": "confirm", "value": "other password"},
        {"change": "confirm"},
        {"click": True},
    ])
    assert state["results"] == [{"blocked": True, "submitted": False, "message": "Passwords do not match."}]
