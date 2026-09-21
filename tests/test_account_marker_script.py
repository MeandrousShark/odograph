"""Node-harness tests for base.html's cross-tab "odograph-account" marker.

Every signed-in page writes its account id to a shared localStorage marker
and reloads itself if another tab changes it -- the mechanism that hides
stale private data the moment a sign-out or account switch happens in
another tab. Sign-in and signup pages extend the same base.html shell with
no account, so merely opening one must not touch the marker, or opening it
in a new tab would make every already-signed-in tab believe the account
changed and reload. A real sign-out (POST /logout) still has to change the
marker so those other tabs notice; app/auth.py's logout redirects to
/login with a `signed_out` query marker for exactly that, read only by
this script, never by the server.
"""
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

ROOT = Path(__file__).parents[1]
TZ = ZoneInfo("UTC")


def _inline_script() -> str:
    """The account-marker script's text does not depend on template context
    (only the `nonce` attribute and the body's `data-account-id` do, and the
    harness mocks `document.body.dataset.accountId` directly), so any render
    is enough to extract it from."""
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    body = templates.env.get_template("base.html").render(
        user=None, csrf="test-csrf-token", csp_nonce="test-nonce",
    )
    start = body.index("const account = document.body.dataset.accountId;")
    start = body.rindex("<script", 0, start)
    start = body.index(">", start) + 1
    end = body.index("</script>", start)
    return body[start:end]


def test_body_data_account_id_reflects_the_signed_in_user_or_is_empty():
    """Cheap sanity check on the Jinja side the harness below cannot see:
    the attribute the script reads is empty exactly when there is no user."""
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    signed_out = templates.env.get_template("base.html").render(
        user=None, csrf="t", csp_nonce="n",
    )
    signed_in = templates.env.get_template("base.html").render(
        user={"id": 42}, csrf="t", csp_nonce="n",
    )
    assert 'data-account-id=""' in signed_out
    assert 'data-account-id="42"' in signed_in


HARNESS = r"""
const vm = require("node:vm");

const scenario = JSON.parse(process.argv[1]);

const store = Object.assign({}, scenario.initialStorage || {});
const removed = [];
const localStorage = {
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem(key, value) { store[key] = String(value); },
  removeItem(key) { delete store[key]; removed.push(key); },
};

const documentListeners = new Map();
const windowListeners = new Map();
function on(map, type, callback) {
  const list = map.get(type) || [];
  list.push(callback);
  map.set(type, list);
}
function emit(map, type, event) {
  for (const callback of map.get(type) || []) callback(event);
}

let hiddenCount = 0;
const style = {};
Object.defineProperty(style, "visibility", {
  set() { hiddenCount += 1; },
  get() { return "hidden"; },
});

const document = {
  body: { dataset: { accountId: scenario.accountId || "" } },
  documentElement: { style },
  addEventListener(type, callback) { on(documentListeners, type, callback); },
};

let reloadCount = 0;
const replaceStateCalls = [];
const window = {
  localStorage,
  location: {
    search: scenario.search || "",
    pathname: scenario.pathname || "/login",
    hash: scenario.hash || "",
    reload() { reloadCount += 1; },
  },
  history: {
    replaceState(state, title, url) {
      replaceStateCalls.push({ state, title, url });
    },
  },
  addEventListener(type, callback) { on(windowListeners, type, callback); },
};

const context = { console, window, document, localStorage, URLSearchParams };
vm.createContext(context);
vm.runInContext(process.argv[2], context, { filename: "account-marker-inline.js" });

if (scenario.emitStorageNewValue !== undefined) {
  emit(windowListeners, "storage", {
    key: "odograph-account", newValue: scenario.emitStorageNewValue,
  });
}

process.stdout.write(JSON.stringify({
  store,
  removed,
  reloadCount,
  hiddenCount,
  replaceStateCalls,
  hasStorageListener: windowListeners.has("storage"),
}));
"""


def _run(**scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; install Node to run the account marker harness")
    script = _inline_script()
    args = [node, "-e", HARNESS, json.dumps(scenario), script]
    try:
        result = subprocess.run(
            args, cwd=ROOT, text=True, capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"account marker Node harness timed out: {exc}")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_opening_a_page_with_no_account_never_touches_the_marker():
    """The bug: base.html used to write the marker unconditionally, so
    merely opening /login or /signup in a new tab -- with no account of its
    own -- overwrote the marker and reloaded every other signed-in tab."""
    result = _run(accountId="", search="", initialStorage={"odograph-account": "42"})
    assert result["store"] == {"odograph-account": "42"}
    # "htmx-history-cache" is removed unconditionally, unrelated to the
    # account marker -- the marker itself must be untouched.
    assert "odograph-account" not in result["removed"]
    assert result["hasStorageListener"] is False


def test_signed_in_page_writes_its_own_account_id():
    """Contrast case: a normal signed-in page (or signing in as a different
    account) still writes the marker, so other tabs do notice."""
    result = _run(accountId="42", search="", initialStorage={})
    assert result["store"] == {"odograph-account": "42"}
    assert result["hasStorageListener"] is True


def test_real_sign_out_clears_the_marker_even_though_the_landing_page_has_no_account():
    """/login?signed_out=1, the redirect target logout uses, must still
    change the marker so other signed-in tabs hide and reload."""
    result = _run(
        accountId="", search="?signed_out=1", pathname="/login",
        initialStorage={"odograph-account": "42"},
    )
    assert "odograph-account" not in result["store"]
    assert "odograph-account" in result["removed"]


def test_sign_out_signal_is_removed_from_the_url_after_handling():
    result = _run(
        accountId="", search="?signed_out=1", pathname="/login", hash="",
        initialStorage={"odograph-account": "42"},
    )
    assert result["replaceStateCalls"] == [{"state": None, "title": "", "url": "/login"}]


def test_sign_out_signal_preserves_other_query_params_and_the_hash():
    result = _run(
        accountId="", search="?foo=bar&signed_out=1", pathname="/login", hash="#panel",
        initialStorage={},
    )
    assert result["replaceStateCalls"] == [
        {"state": None, "title": "", "url": "/login?foo=bar#panel"}
    ]


def test_a_stale_marker_still_reloads_the_tab_once_notified():
    """Sanity check that the surrounding reload mechanism the fix leaves
    alone still works: a signed-in tab reloads when another tab's write
    changes the marker to something else."""
    result = _run(accountId="42", search="", initialStorage={}, emitStorageNewValue="99")
    assert result["reloadCount"] == 1
    assert result["hiddenCount"] >= 1
