"""Pin every threshold docs/detection.md claims to the code's defaults."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import DEFAULT_MISSING_TRIP_GAP_M
from app.detector.core import Params


ROOT = Path(__file__).resolve().parents[1]

# When the default moves, change these two lines and docs/detection.md together.
MISSING_TRIP_GAP_M = 500.0
MISSING_TRIP_GAP_TEXT = "500 m"

STAY_RADIUS_M = 150.0
STAY_MIN_DURATION_S = 300.0
MIN_TRIP_DISTANCE_M = 300.0
GAP_FLAG_THRESHOLD_S = 600.0
WALK_MAX_SPEED_MS = 2.0
ARRIVAL_MIN_DURATION_S = 60.0

_METERS_PER_MILE = 1609.344


def _detection_text() -> str:
    # Hard-wrapped prose: compare phrases with single spaces.
    text = (ROOT / "docs/detection.md").read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", text)


def test_detector_defaults_match_the_documented_values():
    params = Params()

    assert params.stay_radius_m == STAY_RADIUS_M
    assert params.stay_min_duration_s == STAY_MIN_DURATION_S
    assert params.min_trip_distance_m == MIN_TRIP_DISTANCE_M
    assert params.gap_flag_threshold_s == GAP_FLAG_THRESHOLD_S
    assert params.walk_max_speed_ms == WALK_MAX_SPEED_MS
    assert params.arrival_min_duration_s == ARRIVAL_MIN_DURATION_S
    assert params.arrival_triggers == {"c", "v"}


@pytest.mark.parametrize(
    ("variable", "documented"),
    [
        ("STAY_RADIUS_M", STAY_RADIUS_M),
        ("STAY_MIN_DURATION_S", STAY_MIN_DURATION_S),
        ("MIN_TRIP_DISTANCE_M", MIN_TRIP_DISTANCE_M),
        ("GAP_FLAG_THRESHOLD_S", GAP_FLAG_THRESHOLD_S),
        ("WALK_MAX_SPEED_MS", WALK_MAX_SPEED_MS),
    ],
)
def test_runtime_config_uses_the_documented_detector_values(variable, documented):
    # Config repeats these defaults, and its copies are what a running app uses.
    source = (ROOT / "app/config.py").read_text(encoding="utf-8")

    assert f'_f("{variable}", {documented})' in source


def test_missing_trip_default_matches_the_documented_distance():
    assert DEFAULT_MISSING_TRIP_GAP_M == MISSING_TRIP_GAP_M


def test_detection_guide_states_the_pinned_thresholds():
    text = _detection_text()
    walk_kmh = WALK_MAX_SPEED_MS * 3.6
    walk_mph = WALK_MAX_SPEED_MS * 3600 / _METERS_PER_MILE
    min_trip_mi = MIN_TRIP_DISTANCE_M / _METERS_PER_MILE

    assert STAY_MIN_DURATION_S == 5 * 60
    assert ARRIVAL_MIN_DURATION_S == 60
    for phrase in (
        f"about {STAY_RADIUS_M:.0f} m",
        "five minutes",
        f"about {walk_mph:.1f} mph ({walk_kmh:.1f} km/h)",
        "about a minute",
        f"Gaps over {GAP_FLAG_THRESHOLD_S / 60:.0f} minutes",
        f"about {MIN_TRIP_DISTANCE_M:.0f} m ({min_trip_mi:.1f} mi)",
        f"more than {MISSING_TRIP_GAP_TEXT} from where the last one ended",
        f"end up more than {MISSING_TRIP_GAP_TEXT} apart",
    ):
        assert phrase in text


def test_readme_short_answers_state_the_pinned_thresholds():
    readme = re.sub(r"\s+", " ", (ROOT / "README.md").read_text(encoding="utf-8"))
    walk_mph = WALK_MAX_SPEED_MS * 3600 / _METERS_PER_MILE

    assert f"about {walk_mph:.1f} mph" in readme
    assert f"over {GAP_FLAG_THRESHOLD_S / 60:.0f} minutes" in readme
    assert "docs/detection.md" in readme
