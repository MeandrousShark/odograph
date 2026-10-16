"""Helpers for asserting that workflow actions are pinned to full commit SHAs."""

from __future__ import annotations

import re

PINNED_USES = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)@(?P<sha>[0-9a-f]{40})$")


def action_name(uses: str) -> str:
    """Return the owner/repo of a SHA-pinned action; fail on any other ref."""
    match = PINNED_USES.fullmatch(uses)
    assert match, f"action is not pinned to a 40-character commit SHA: {uses!r}"
    return match.group("name")


def is_pinned(uses: str | None, name: str) -> bool:
    """True when `uses` is `name` pinned to a full commit SHA."""
    if uses is None:
        return False
    match = PINNED_USES.fullmatch(uses)
    return bool(match) and match.group("name") == name


def pinned_names(uses_values) -> set[str]:
    """The set of action names in `uses_values`, each required to be SHA-pinned."""
    return {action_name(value) for value in uses_values}
