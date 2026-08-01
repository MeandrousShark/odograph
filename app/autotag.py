"""Pure kind-based auto-tagging logic.

A trip's start/end are each either a resolved place `(place_id, kind)` or
`None` (unresolved — no place within radius, or a manual trip with no
geometry at all). A rule matches a trip if its two sides can be paired with
the trip's two ends in either order (direction-agnostic); the side that
requires a specific place is worth more than one that only requires a kind,
which in turn beats an unconstrained "any" side. Best total specificity
wins; ties go to the oldest rule (lowest id, since ids are assigned in
insertion order).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

PlaceRef = Optional[Tuple[int, str]]  # (place_id, kind), or None if unresolved


@dataclass(frozen=True)
class Rule:
    id: int
    a_place: int | None
    a_kind: str | None
    b_place: int | None
    b_kind: str | None
    category: str


@dataclass(frozen=True)
class AutotagTrip:
    id: int
    category: str
    tag_source: str | None
    start_place: PlaceRef
    end_place: PlaceRef


@dataclass(frozen=True)
class AutotagResult:
    trip_id: int
    category: str
    tag_source: str | None  # always 'rule', or None on a revert to unclassified


def _side_specificity(place: PlaceRef, side_place: int | None, side_kind: str | None) -> int | None:
    """None = this side doesn't match `place`. A specific-place side (2) also
    requires the place be resolved; a kind side (1) likewise; "any" (0)
    matches whether the trip end is resolved or not.
    """
    if side_place is not None:
        return 2 if (place is not None and place[0] == side_place) else None
    if side_kind is not None:
        return 1 if (place is not None and place[1] == side_kind) else None
    return 0


def _rule_specificity(rule: Rule, start: PlaceRef, end: PlaceRef) -> int | None:
    best: int | None = None
    for p1, side1, p2, side2 in (
        (start, (rule.a_place, rule.a_kind), end, (rule.b_place, rule.b_kind)),
        (end, (rule.a_place, rule.a_kind), start, (rule.b_place, rule.b_kind)),
    ):
        s1 = _side_specificity(p1, *side1)
        s2 = _side_specificity(p2, *side2)
        if s1 is not None and s2 is not None:
            total = s1 + s2
            if best is None or total > best:
                best = total
    return best


def match_rule(rules: list[Rule], start: PlaceRef, end: PlaceRef) -> Rule | None:
    """The best-matching rule for a trip's start/end places, or None."""
    best_rule: Rule | None = None
    best_score: int | None = None
    for rule in rules:
        score = _rule_specificity(rule, start, end)
        if score is None:
            continue
        if (
            best_score is None
            or score > best_score
            or (score == best_score and rule.id < best_rule.id)  # type: ignore[union-attr]
        ):
            best_rule, best_score = rule, score
    return best_rule


def plan_autotags(trips: list[AutotagTrip], rules: list[Rule]) -> list[AutotagResult]:
    """Decide category/tag_source changes for trips whose tag isn't
    human-owned. A trip with `tag_source == 'human'` is never touched, even
    if passed in here — that's the one line the human-tag-supremacy
    invariant rests on, so it's enforced defensively at this layer too, not
    just by the caller filtering its input.
    """
    results = []
    for trip in trips:
        if trip.tag_source == "human":
            continue
        rule = match_rule(rules, trip.start_place, trip.end_place)
        if rule is not None:
            if trip.category != rule.category or trip.tag_source != "rule":
                results.append(AutotagResult(trip.id, rule.category, "rule"))
        elif trip.tag_source == "rule":
            # Was auto-tagged, no rule matches anymore (e.g. the matching
            # rule or place was deleted) -> revert rather than leave stale.
            results.append(AutotagResult(trip.id, "unclassified", None))
    return results
