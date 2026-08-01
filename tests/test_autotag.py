"""Tests for kind-based auto-tagging."""
from __future__ import annotations

from app.autotag import AutotagResult, AutotagTrip, Rule, match_rule, plan_autotags

HOME_WORK = Rule(id=1, a_place=None, a_kind="home", b_place=None, b_kind="work", category="personal")
WORK_WORK = Rule(id=2, a_place=None, a_kind="work", b_place=None, b_kind="work", category="business")


def test_direction_agnostic():
    home = (1, "home")
    work = (2, "work")
    assert match_rule([HOME_WORK], home, work) == HOME_WORK
    assert match_rule([HOME_WORK], work, home) == HOME_WORK  # reversed


def test_kind_matches_any_place_of_that_kind():
    site_a = (5, "work")
    site_b = (7, "work")  # a different work place than site_a
    assert match_rule([WORK_WORK], site_a, site_b) == WORK_WORK


def test_specificity_pair_beats_kind():
    # Side A: specific place #100 (2 pts). Side B: kind 'work' (1 pt).
    specific_vs_kind = Rule(id=3, a_place=100, a_kind=None, b_place=None, b_kind="work", category="business")
    start = (100, "home")  # matches specific_vs_kind's side A by id
    end = (7, "work")      # matches side B by kind, and also matches HOME_WORK's kind side
    winner = match_rule([HOME_WORK, specific_vs_kind], start, end)
    assert winner == specific_vs_kind  # 2+1=3 beats HOME_WORK's 1+1=2


def test_specificity_kind_beats_any():
    any_side_rule = Rule(id=4, a_place=None, a_kind=None, b_place=None, b_kind="work", category="business")
    start = (1, "home")
    end = (2, "work")
    # HOME_WORK scores 1+1=2; any_side_rule scores 0+1=1 (home side is "any").
    assert match_rule([HOME_WORK, any_side_rule], start, end) == HOME_WORK


def test_tie_breaks_to_oldest_rule():
    duplicate = Rule(id=99, a_place=None, a_kind="home", b_place=None, b_kind="work", category="business")
    home, work = (1, "home"), (2, "work")
    # Same specificity (2) as HOME_WORK (id=1); lower id wins.
    assert match_rule([duplicate, HOME_WORK], home, work) == HOME_WORK


def test_unresolved_place_only_matches_any():
    any_rule = Rule(id=5, a_place=None, a_kind=None, b_place=None, b_kind="work", category="business")
    # start is unresolved (None) -> can't satisfy a kind or specific-place side...
    assert match_rule([HOME_WORK], None, (2, "work")) is None
    # ...but does satisfy an "any" side.
    assert match_rule([any_rule], None, (2, "work")) == any_rule


def test_plan_autotags_applies_matching_rule():
    trip = AutotagTrip(id=1, category="unclassified", tag_source=None,
                        start_place=(1, "home"), end_place=(2, "work"))
    result = plan_autotags([trip], [HOME_WORK])
    assert result == [AutotagResult(1, "personal", "rule")]


def test_human_tag_source_never_touched():
    trip = AutotagTrip(id=1, category="business", tag_source="human",
                        start_place=(1, "home"), end_place=(2, "work"))
    # Even though HOME_WORK would say "personal", a human tag is untouchable.
    assert plan_autotags([trip], [HOME_WORK]) == []


def test_orphaned_rule_tag_reverts():
    # Previously auto-tagged 'business' by a rule that no longer exists/matches.
    trip = AutotagTrip(id=1, category="business", tag_source="rule",
                        start_place=(1, "home"), end_place=(99, "other"))
    results = plan_autotags([trip], [HOME_WORK])
    assert len(results) == 1
    assert results[0].trip_id == 1
    assert results[0].category == "unclassified"
    assert results[0].tag_source is None


def test_no_op_when_unclassified_and_no_rule_matches():
    trip = AutotagTrip(id=1, category="unclassified", tag_source=None,
                        start_place=(1, "home"), end_place=(99, "other"))
    assert plan_autotags([trip], [HOME_WORK]) == []


def test_no_op_when_already_correctly_rule_tagged():
    trip = AutotagTrip(id=1, category="personal", tag_source="rule",
                        start_place=(1, "home"), end_place=(2, "work"))
    assert plan_autotags([trip], [HOME_WORK]) == []
