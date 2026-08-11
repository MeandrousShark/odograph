"""Config.from_env's auth-path validation matrix and signup fail-closed default.
OIDC becoming optional must not touch the pre-existing required-vars check.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import (
    DEFAULT_MAP_TILE_ATTRIBUTION,
    DEFAULT_MAP_TILE_URL,
    DEFAULT_MISSING_TRIP_GAP_M,
    Config,
)
from app.diagnose import worker_reports_from_config
from app.geocode import GeoapifyProvider, NominatimProvider
from app.main import make_templates

REQUIRED_VARS = ("DATABASE_URL", "INGEST_PASSWORD", "SESSION_SECRET")
OIDC_VARS = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")
ALL_OPTIONAL_AUTH_VARS = OIDC_VARS + (
    "ADMIN_TOKEN", "INITIAL_ADMIN_SIGNUP", "DEV_NO_AUTH",
)
RUNTIME_IDENTITY_VARS = ("APP_VERSION", "APP_GIT_REVISION")
WORKER_ENV_VARS = (
    "OSRM_URL", "RAW_MESSAGE_RETENTION_DAYS", "NTFY_URL", "NTFY_TOPIC",
    "ODOMETER_REMINDER", "SMTP_HOST", "EMAIL_FROM", "EMAIL_TO",
    "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "GEOCODE_NOMINATIM_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Sets the three always-required vars and clears every auth-relevant
    optional var, so each test starts from a known-empty auth config
    regardless of what's exported in the real shell environment.
    """
    for key, value in zip(REQUIRED_VARS, ("postgresql://x/x", "ingest-pw", "session-secret")):
        monkeypatch.setenv(key, value)
    for key in ALL_OPTIONAL_AUTH_VARS + RUNTIME_IDENTITY_VARS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_missing_required_vars_still_raises(monkeypatch):
    for key in REQUIRED_VARS + OIDC_VARS + ("ADMIN_TOKEN", "DEV_NO_AUTH"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        Config.from_env()


def test_dev_no_auth_needs_no_oidc(clean_env):
    clean_env.setenv("DEV_NO_AUTH", "1")
    cfg = Config.from_env()
    assert cfg.dev_no_auth is True
    assert cfg.oidc_configured is False
    assert cfg.initial_admin_signup is False


def test_local_only_mode_needs_no_oidc(clean_env):
    cfg = Config.from_env()
    assert cfg.dev_no_auth is False
    assert cfg.oidc_configured is False
    assert cfg.initial_admin_signup is False


def test_oidc_only_mode_defaults_signup_closed(clean_env):
    clean_env.setenv("OIDC_ISSUER", "https://idp.example.com")
    clean_env.setenv("OIDC_CLIENT_ID", "client-id")
    clean_env.setenv("OIDC_CLIENT_SECRET", "client-secret")
    cfg = Config.from_env()
    assert cfg.oidc_configured is True
    assert cfg.initial_admin_signup is False


def test_oidc_and_initial_signup_config_can_coexist(clean_env):
    clean_env.setenv("OIDC_ISSUER", "https://idp.example.com")
    clean_env.setenv("OIDC_CLIENT_ID", "client-id")
    clean_env.setenv("OIDC_CLIENT_SECRET", "client-secret")
    clean_env.setenv("INITIAL_ADMIN_SIGNUP", "1")
    cfg = Config.from_env()
    assert cfg.oidc_configured is True
    assert cfg.initial_admin_signup is True


def test_missing_initial_signup_is_fail_closed(clean_env):
    cfg = Config.from_env()
    assert cfg.oidc_configured is False
    assert cfg.initial_admin_signup is False


def test_obsolete_admin_token_is_ignored(clean_env):
    clean_env.setenv("ADMIN_TOKEN", "obsolete-value")
    cfg = Config.from_env()
    assert cfg.initial_admin_signup is False


@pytest.mark.parametrize("set_var", OIDC_VARS)
def test_partial_oidc_config_raises(clean_env, set_var):
    # Some but not all of the three OIDC vars set is neither a working
    # provider nor a clean absence -- almost certainly an operator typo.
    clean_env.setenv(set_var, "just-one-var-set")
    with pytest.raises(RuntimeError, match="OIDC_ISSUER, OIDC_CLIENT_ID, and OIDC_CLIENT_SECRET"):
        Config.from_env()


def test_partial_oidc_config_is_allowed_under_dev_no_auth(clean_env):
    # DEV_NO_AUTH bypasses the OIDC/local-login decision entirely, so a
    # half-set OIDC block left over from a prior config doesn't block a
    # dev bring-up.
    clean_env.setenv("DEV_NO_AUTH", "1")
    clean_env.setenv("OIDC_ISSUER", "https://idp.example.com")
    cfg = Config.from_env()
    assert cfg.dev_no_auth is True


def test_runtime_identity_defaults_are_honest_for_source_builds(clean_env):
    cfg = Config.from_env()

    assert cfg.app_version == "dev"
    assert cfg.app_git_revision == "unknown"


def test_runtime_identity_reads_image_environment(clean_env):
    clean_env.setenv("APP_VERSION", "v0.6.0-rc.1")
    clean_env.setenv("APP_GIT_REVISION", "0123456789abcdef")

    cfg = Config.from_env()

    assert cfg.app_version == "v0.6.0-rc.1"
    assert cfg.app_git_revision == "0123456789abcdef"


def test_map_tile_and_hsts_default_to_todays_values(clean_env):
    clean_env.delenv("MAP_TILE_URL", raising=False)
    clean_env.delenv("MAP_TILE_ATTRIBUTION", raising=False)
    clean_env.delenv("HSTS_MAX_AGE", raising=False)

    cfg = Config.from_env()

    assert cfg.map_tile_url == DEFAULT_MAP_TILE_URL
    assert cfg.map_tile_attribution == DEFAULT_MAP_TILE_ATTRIBUTION
    assert cfg.missing_trip_gap_m == DEFAULT_MISSING_TRIP_GAP_M
    assert cfg.map_tile_host == "https://tile.openstreetmap.org"
    assert cfg.hsts_max_age == 0


def test_template_only_config_uses_shared_map_and_missing_trip_defaults():
    templates = make_templates(SimpleNamespace(display_tz=timezone.utc, app_version="test"))

    assert templates.env.globals["map_tile_url"] == DEFAULT_MAP_TILE_URL
    assert templates.env.globals["map_tile_attribution"] == DEFAULT_MAP_TILE_ATTRIBUTION

    trip = {
        "id": 2,
        "started_at": datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
        "prev_trip_ended_at": datetime(2026, 8, 10, 11, tzinfo=timezone.utc),
        "prev_end_gap_m": DEFAULT_MISSING_TRIP_GAP_M + 1,
        "prev_trip_end_lat": 47.0,
        "prev_trip_end_lon": -122.0,
        "prev_trip_end_place_name": None,
        "missing_trip_covered": False,
    }
    assert templates.env.globals["missing_trip_badge"](trip) is not None


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, (False, True, False, False, False)),
        ({"OSRM_URL": "http://osrm.internal:5000"}, (True, True, False, False, False)),
        ({"RAW_MESSAGE_RETENTION_DAYS": "0"}, (False, False, False, False, False)),
        ({"NTFY_URL": "http://ntfy.internal"}, (False, True, False, False, False)),
        ({"NTFY_TOPIC": "trips"}, (False, True, False, False, False)),
        (
            {"NTFY_URL": "http://ntfy.internal", "NTFY_TOPIC": "trips"},
            (False, True, True, True, False),
        ),
        (
            {
                "NTFY_URL": "http://ntfy.internal", "NTFY_TOPIC": "trips",
                "ODOMETER_REMINDER": "0",
            },
            (False, True, True, False, False),
        ),
        (
            {"NTFY_URL": "http://ntfy.internal", "ODOMETER_REMINDER": "1"},
            (False, True, False, False, False),
        ),
        (
            {"SMTP_HOST": "smtp.internal", "EMAIL_FROM": "from@example.com"},
            (False, True, False, False, False),
        ),
        (
            {
                "SMTP_HOST": "smtp.internal", "EMAIL_FROM": "from@example.com",
                "EMAIL_TO": "to@example.com",
            },
            (False, True, False, False, True),
        ),
    ],
)
def test_config_and_diagnostics_worker_enablement_matrix(clean_env, environment, expected):
    for name in WORKER_ENV_VARS:
        clean_env.delenv(name, raising=False)
    for name, value in environment.items():
        clean_env.setenv(name, value)

    cfg = Config.from_env()

    assert (
        cfg.snap_enabled,
        cfg.retention_enabled,
        cfg.nudge_enabled,
        cfg.odometer_reminder_enabled,
        cfg.email_enabled,
    ) == expected
    reports = {report.name: report.enabled for report in worker_reports_from_config(cfg)}
    assert (
        reports["snap"],
        reports["retention"],
        reports["nudge"],
        reports["odometer_reminder"],
        reports["email_digest"],
    ) == expected


def test_map_tile_host_follows_a_configured_map_tile_url(clean_env):
    clean_env.setenv("MAP_TILE_URL", "https://tiles.example.net/{z}/{x}/{y}.png")

    cfg = Config.from_env()

    assert cfg.map_tile_host == "https://tiles.example.net"


def test_hsts_max_age_reads_from_environment(clean_env):
    clean_env.setenv("HSTS_MAX_AGE", "63072000")

    cfg = Config.from_env()

    assert cfg.hsts_max_age == 63072000


@pytest.mark.parametrize("value", ["*", ""])
def test_forwarded_allow_ips_wildcard_or_empty_warns(clean_env, caplog, value):
    clean_env.setenv("FORWARDED_ALLOW_IPS", value)
    with caplog.at_level("WARNING"):
        Config.from_env()
    assert any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)


def test_forwarded_allow_ips_unset_warns_too(clean_env, caplog):
    # Unset behaves the same as explicitly empty once it reaches the app --
    # the compose file always sets the variable from .env, so an operator
    # who never filled it in gets an empty string in the container, not
    # Uvicorn's own safe 127.0.0.1 default.
    clean_env.delenv("FORWARDED_ALLOW_IPS", raising=False)
    with caplog.at_level("WARNING"):
        Config.from_env()
    assert any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)


@pytest.mark.parametrize("value", ["127.0.0.1", "10.0.0.5/32", "203.0.113.9"])
def test_forwarded_allow_ips_scoped_value_does_not_warn(clean_env, caplog, value):
    clean_env.setenv("FORWARDED_ALLOW_IPS", value)
    with caplog.at_level("WARNING"):
        Config.from_env()
    assert not any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)


def _clean_geocode_env(monkeypatch):
    for key in (
        "GEOCODE_API_KEY", "GEOCODE_PROVIDER", "GEOCODE_OMIT_COUNTRY", "GEOCODE_NOMINATIM_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_geocode_provider_unset_and_no_api_key_disables_geocoding(clean_env):
    _clean_geocode_env(clean_env)
    cfg = Config.from_env()
    assert cfg.geocode_provider is None


def test_geocode_provider_unset_falls_back_to_geoapify_when_api_key_set(clean_env):
    # Upgrade compatibility (D1): production ran with only GEOCODE_API_KEY
    # set before GEOCODE_PROVIDER existed -- an existing .env must keep
    # geocoding with zero edits.
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_API_KEY", "a-real-key")
    cfg = Config.from_env()
    assert isinstance(cfg.geocode_provider, GeoapifyProvider)
    assert cfg.geocode_provider.api_key == "a-real-key"


def test_geocode_provider_explicit_geoapify_works(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_PROVIDER", "geoapify")
    clean_env.setenv("GEOCODE_API_KEY", "a-real-key")
    cfg = Config.from_env()
    assert isinstance(cfg.geocode_provider, GeoapifyProvider)


def test_geocode_provider_unrecognised_value_fails_fast_at_startup(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_PROVIDER", "bogus")
    with pytest.raises(RuntimeError, match="Unrecognised GEOCODE_PROVIDER"):
        Config.from_env()


def test_geocode_provider_unrecognised_value_names_accepted_values(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_PROVIDER", "bogus")
    with pytest.raises(RuntimeError, match="geoapify") as exc_info:
        Config.from_env()
    assert "nominatim" in str(exc_info.value)


def test_geocode_provider_nominatim_without_url_fails_fast(clean_env):
    # "nominatim" is a recognised GEOCODE_PROVIDER value (D1), but it ships
    # no default URL (D3) -- Config.from_env() itself must not fail fast on
    # the name alone, but building the actual provider object without a URL
    # still surfaces a clear error rather than silently disabling geocoding.
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_PROVIDER", "nominatim")
    cfg = Config.from_env()
    assert cfg.geocode_provider_name == "nominatim"
    with pytest.raises(RuntimeError, match="GEOCODE_NOMINATIM_URL"):
        cfg.geocode_provider


def test_geocode_provider_nominatim_with_url_builds(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_PROVIDER", "nominatim")
    clean_env.setenv("GEOCODE_NOMINATIM_URL", "http://nominatim.internal:8080")
    cfg = Config.from_env()
    provider = cfg.geocode_provider
    assert isinstance(provider, NominatimProvider)
    assert provider.base_url == "http://nominatim.internal:8080"


def test_geocode_omit_country_defaults_to_united_states_of_america(clean_env):
    _clean_geocode_env(clean_env)
    cfg = Config.from_env()
    assert cfg.geocode_omit_country == "United States of America"


def test_geocode_omit_country_reads_a_custom_value(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_OMIT_COUNTRY", "Canada")
    cfg = Config.from_env()
    assert cfg.geocode_omit_country == "Canada"


def test_geocode_omit_country_empty_disables_stripping(clean_env):
    _clean_geocode_env(clean_env)
    clean_env.setenv("GEOCODE_OMIT_COUNTRY", "")
    cfg = Config.from_env()
    assert cfg.geocode_omit_country == ""


def test_forwarded_allow_ips_wildcard_is_silent_under_dev_no_auth(clean_env, caplog):
    # DEV_NO_AUTH is a local-only escape hatch -- it must not gain a second,
    # unrelated startup warning that would make dev bring-up noisier without
    # protecting anything a dev instance actually exposes.
    clean_env.setenv("DEV_NO_AUTH", "1")
    clean_env.setenv("FORWARDED_ALLOW_IPS", "*")
    with caplog.at_level("WARNING"):
        Config.from_env()
    assert not any("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)
