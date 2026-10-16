"""Every third-party workflow action is pinned to a full commit SHA."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

SHA_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
COMMENT_TAG = re.compile(r"^\s+#\s+(?P<tag>v[0-9][0-9.]*)\s*$")

# The release each pin was resolved from, recorded as the trailing comment.
EXPECTED_TAGS = {
    "actions/checkout": "v6",
    "actions/setup-python": "v6",
    "actions/upload-artifact": "v4",
    "actions/download-artifact": "v4",
    "docker/setup-qemu-action": "v4",
    "docker/setup-buildx-action": "v4",
    "docker/login-action": "v4",
    "docker/build-push-action": "v7",
    "aquasecurity/trivy-action": "v0.36.0",
    "anchore/sbom-action": "v0.24.0",
    "sigstore/cosign-installer": "v4.1.2",
    "pypa/gh-action-pip-audit": "v1.1.0",
}


def _uses_values(node) -> list[str]:
    """Every `uses:` value in a parsed workflow, at job or step level."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_uses_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_uses_values(item))
    return found


def _non_local(values: list[str]) -> list[str]:
    return [value for value in values if not value.startswith("./")]


def test_workflows_exist():
    assert WORKFLOWS


def test_every_non_local_action_is_pinned_to_a_full_commit_sha():
    unpinned = []
    for path in WORKFLOWS:
        parsed = yaml.safe_load(path.read_text())
        for value in _non_local(_uses_values(parsed)):
            if not SHA_REF.fullmatch(value):
                unpinned.append(f"{path.name}: {value}")

    assert unpinned == []


def test_pinned_actions_carry_their_release_tag_in_a_trailing_comment():
    problems = []
    seen = set()
    for path in WORKFLOWS:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = USES_LINE.match(line)
            if not match or match.group("ref").startswith("./"):
                continue
            name = match.group("ref").split("@", 1)[0]
            seen.add(name)
            comment = COMMENT_TAG.match(match.group("rest"))
            expected = EXPECTED_TAGS.get(name)
            if expected is None:
                problems.append(f"{path.name}:{number}: unexpected action {name}")
            elif not comment or comment.group("tag") != expected:
                problems.append(f"{path.name}:{number}: {name} needs a '# {expected}' comment")

    assert problems == []
    assert seen == set(EXPECTED_TAGS)


def test_scanner_rejects_tag_and_branch_refs():
    workflow = yaml.safe_load(
        "jobs:\n  a:\n    steps:\n"
        "      - uses: actions/checkout@v6\n"
        "      - uses: actions/setup-python@main\n"
        "      - uses: actions/cache@" + "a" * 39 + "\n"
        "      - uses: ./.github/workflows/local.yml\n"
        "      - uses: actions/upload-artifact@" + "b" * 40 + " # v4\n"
    )

    values = _non_local(_uses_values(workflow))

    assert [value for value in values if not SHA_REF.fullmatch(value)] == [
        "actions/checkout@v6",
        "actions/setup-python@main",
        "actions/cache@" + "a" * 39,
    ]
