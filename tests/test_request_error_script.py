"""Node-harness tests for base.html's shared htmx request-error banner.

htmx 1.9 does not swap 4xx/5xx responses, so before B26 a refused or failed
request (a rejected split, a proxy timeout) left the page looking as if
nothing had happened. The banner shows the server's `detail` as plain text,
falls back to the HTTP status, and reports a network failure separately.
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


def _render_base() -> str:
    templates = make_templates(SimpleNamespace(display_tz=TZ, app_version="test"))
    return templates.env.get_template("base.html").render(
        user={"id": 42, "name": "Tester"}, csrf="test-csrf-token", csp_nonce="test-nonce",
    )


def _inline_script() -> str:
    body = _render_base()
    start = body.index('var box = document.getElementById("request-error");')
    start = body.rindex("<script", 0, start)
    start = body.index(">", start) + 1
    end = body.index("</script>", start)
    return body[start:end]


def test_banner_markup_is_a_hidden_alert_with_a_dismiss_control():
    body = _render_base()
    banner = body.split('<div id="request-error"', 1)[1].split("</div>", 1)[0]

    assert 'class="notice notice-danger request-error" role="alert" hidden>' in banner
    assert '<p id="request-error-message"></p>' in banner
    assert "data-request-error-dismiss" in banner
    assert body.index('id="request-error"') < body.index('var box = document.getElementById("request-error");')


HARNESS = r"""
const vm = require("node:vm");

const steps = JSON.parse(process.argv[1]);
const listeners = new Map();
const box = { hidden: true };
const text = { textContent: "" };
const document = {
  getElementById(id) {
    return id === "request-error" ? box : id === "request-error-message" ? text : null;
  },
  addEventListener(type, callback) {
    const list = listeners.get(type) || [];
    list.push(callback);
    listeners.set(type, list);
  },
};

const context = { document, JSON, Array };
vm.createContext(context);
vm.runInContext(process.argv[2], context, { filename: "request-error-inline.js" });

const seen = [];
for (const step of steps) {
  const event = { detail: { xhr: { status: step.status, responseText: step.body || "" } } };
  if (step.type === "click") {
    event.target = { closest: () => (step.dismiss ? {} : null) };
  }
  for (const callback of listeners.get(step.type) || []) callback(event);
  seen.push({ hidden: box.hidden, message: text.textContent });
}
process.stdout.write(JSON.stringify(seen));
"""


def _run(steps: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", HARNESS, json.dumps(steps), _inline_script()],
        capture_output=True, text=True, check=True, cwd=ROOT,
    )
    return json.loads(result.stdout)


def test_rejected_request_shows_the_server_detail_as_text():
    detail = "Split point is too close to the start or end of the trip <b>x</b>"
    [seen] = _run([
        {"type": "htmx:responseError", "status": 400, "body": json.dumps({"detail": detail})},
    ])

    assert seen == {"hidden": False, "message": "Error: " + detail}


def test_validation_list_and_non_json_failures_use_generic_messages():
    invalid, gateway = _run([
        {"type": "htmx:responseError", "status": 422,
         "body": json.dumps({"detail": [{"loc": ["body", "point_id"]}]})},
        {"type": "htmx:responseError", "status": 504, "body": "<html>Gateway Timeout</html>"},
    ])

    assert invalid["message"] == "Error: Some of the submitted values are invalid."
    assert gateway["message"] == (
        "Error (HTTP 504). Reload the page and try again."
    )


def test_network_failure_has_its_own_message():
    [seen] = _run([{"type": "htmx:sendError"}])

    assert seen == {
        "hidden": False,
        "message": "Odograph could not be reached. Check the connection and try again.",
    }


def test_next_request_and_dismiss_hide_the_banner():
    _, retried, _, other_click, dismissed = _run([
        {"type": "htmx:sendError"},
        {"type": "htmx:beforeRequest"},
        {"type": "htmx:sendError"},
        {"type": "click", "dismiss": False},
        {"type": "click", "dismiss": True},
    ])

    assert retried["hidden"] is True
    assert other_click["hidden"] is False
    assert dismissed["hidden"] is True
