"""Tests for reverse-geocoding/autocomplete pure logic and each provider's
thin I/O wrapper, plus a shared provider-contract test parametrized over
both implementations so they cannot drift in how they signal "no address
here" (a cacheable miss) versus "ask again later" (an error) -- the
distinction `GeocodeWorker` depends on.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

import httpx
import pytest

from app.geocode import (
    GEOCODE_PRECISION,
    GeoapifyProvider,
    NominatimProvider,
    parse_geoapify_autocomplete_response,
    parse_geoapify_reverse_response,
    parse_nominatim_autocomplete_response,
    parse_nominatim_reverse_response,
    round_coord,
    strip_country_suffix,
)
from app.ui import TRIP_COLUMNS

US_SUFFIX = "United States of America"

# ---- round_coord ----


def test_round_coord_rounds_to_precision():
    assert round_coord(47.60312345, -122.33019876) == (47.6031, -122.3302)


def test_round_coord_matches_python_round_semantics():
    # Delegates straight to `round()` (float repr, not decimal); this just
    # pins that behavior rather than asserting a particular half-rounding
    # direction, which float imprecision makes unreliable to hardcode.
    assert round_coord(47.60315, -122.33015) == (round(47.60315, 4), round(-122.33015, 4))


def test_round_coord_exact_precision_unchanged():
    assert round_coord(47.6031, -122.3301) == (47.6031, -122.3301)


def test_trip_columns_address_lookup_matches_geocode_precision():
    assert TRIP_COLUMNS.count(f"::numeric, {GEOCODE_PRECISION})") == 4


# ---- strip_country_suffix ----


def test_strip_country_suffix_drops_the_configured_trailing_suffix():
    label = "400 Broad St, Seattle, WA 98109, United States of America"
    assert strip_country_suffix(label, US_SUFFIX) == "400 Broad St, Seattle, WA 98109"


def test_strip_country_suffix_empty_suffix_disables_stripping():
    label = "400 Broad St, Seattle, WA 98109, United States of America"
    assert strip_country_suffix(label, "") == label


def test_strip_country_suffix_is_provider_agnostic():
    # No metadata argument at all -- any provider's label string strips the
    # same way, per D4.
    assert strip_country_suffix("123 Rue de Rivoli, Paris, France", "France") == (
        "123 Rue de Rivoli, Paris"
    )


def test_strip_country_suffix_only_matches_a_trailing_occurrence():
    for label in (
        "United States of America, Suite 100",
        "Museum of United States of America",
        "123 Main St, United States of America Annex",
    ):
        assert strip_country_suffix(label, US_SUFFIX) == label


def test_strip_country_suffix_no_match_returns_label_unchanged():
    label = "123 Rue de Rivoli, Paris, France"
    assert strip_country_suffix(label, US_SUFFIX) == label


# ---- parse_geoapify_reverse_response ----

REVERSE_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "name": "Space Needle",
                "formatted": "400 Broad St, Seattle, WA 98109, United States of America",
                "lat": 47.6205,
                "lon": -122.3493,
            },
        }
    ],
}


def test_parse_geoapify_reverse_response_extracts_formatted_address():
    assert parse_geoapify_reverse_response(REVERSE_FIXTURE, US_SUFFIX) == (
        "400 Broad St, Seattle, WA 98109"
    )


def test_parse_geoapify_reverse_response_empty_features_returns_none():
    assert parse_geoapify_reverse_response(
        {"type": "FeatureCollection", "features": []}, US_SUFFIX
    ) is None


def test_parse_geoapify_reverse_response_missing_features_key_returns_none():
    assert parse_geoapify_reverse_response({}, US_SUFFIX) is None


def test_parse_geoapify_reverse_response_omit_country_empty_keeps_full_label():
    assert parse_geoapify_reverse_response(REVERSE_FIXTURE, "") == (
        "400 Broad St, Seattle, WA 98109, United States of America"
    )


def test_parse_geoapify_reverse_response_honors_a_custom_suffix():
    body = {"features": [{"properties": {"formatted": "10 Rue Principale, Paris, France"}}]}
    assert parse_geoapify_reverse_response(body, "France") == "10 Rue Principale, Paris"


# ---- parse_geoapify_autocomplete_response ----

AUTOCOMPLETE_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.3493, 47.6205]},
            "properties": {
                "formatted": "400 Broad St, Seattle, WA 98109, United States of America",
                "lat": 47.6205,
                "lon": -122.3493,
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.335, 47.608]},
            "properties": {
                "formatted": "600 4th Ave, Seattle, WA 98104, United States of America",
                "lat": 47.608,
                "lon": -122.335,
            },
        },
    ],
}


def test_parse_geoapify_autocomplete_response_extracts_label_lat_lon():
    results = parse_geoapify_autocomplete_response(AUTOCOMPLETE_FIXTURE, US_SUFFIX)
    assert results == [
        {"label": "400 Broad St, Seattle, WA 98109", "lat": 47.6205, "lon": -122.3493},
        {"label": "600 4th Ave, Seattle, WA 98104", "lat": 47.608, "lon": -122.335},
    ]


def test_parse_geoapify_autocomplete_response_empty_features_returns_empty_list():
    assert parse_geoapify_autocomplete_response(
        {"type": "FeatureCollection", "features": []}, US_SUFFIX
    ) == []


def test_parse_geoapify_autocomplete_response_drops_feature_missing_fields():
    body = {
        "features": [
            {"properties": {"formatted": "Incomplete result"}},  # missing lat/lon
            {"properties": {"formatted": "Complete", "lat": 1.0, "lon": 2.0}},
        ]
    }
    assert parse_geoapify_autocomplete_response(body, US_SUFFIX) == [
        {"label": "Complete", "lat": 1.0, "lon": 2.0}
    ]


def test_parse_geoapify_autocomplete_response_omit_country_empty_keeps_full_label():
    body = {
        "features": [
            {
                "properties": {
                    "formatted": "400 Broad St, Seattle, WA 98109, United States of America",
                    "lat": 47.6205,
                    "lon": -122.3493,
                }
            }
        ]
    }
    assert parse_geoapify_autocomplete_response(body, "") == [
        {
            "label": "400 Broad St, Seattle, WA 98109, United States of America",
            "lat": 47.6205,
            "lon": -122.3493,
        }
    ]


# ---- GeoapifyProvider: thin I/O wrapper ----


def test_geoapify_provider_reverse_sends_the_api_key_and_strips_the_suffix():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "apiKey=secret-key" in str(request.url)
        return httpx.Response(200, json=REVERSE_FIXTURE)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeoapifyProvider(api_key="secret-key", omit_country=US_SUFFIX)
            return await provider.reverse(client, 47.6205, -122.3493)

    assert asyncio.run(scenario()) == "400 Broad St, Seattle, WA 98109"


def test_geoapify_provider_autocomplete_sends_the_query_and_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["text"] == "broad st"
        assert request.url.params["limit"] == "3"
        assert request.url.params["apiKey"] == "secret-key"
        return httpx.Response(200, json=AUTOCOMPLETE_FIXTURE)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GeoapifyProvider(api_key="secret-key", omit_country=US_SUFFIX)
            return await provider.autocomplete(client, "broad st", limit=3)

    results = asyncio.run(scenario())
    assert results[0]["label"] == "400 Broad St, Seattle, WA 98109"


def test_geoapify_provider_name_is_geoapify():
    assert GeoapifyProvider(api_key="k", omit_country="").name == "geoapify"


# ---- parse_nominatim_reverse_response ----

NOMINATIM_REVERSE_FIXTURE = {
    "place_id": 12345,
    "display_name": "400 Broad St, Seattle, WA 98109, United States of America",
    "lat": "47.6205",
    "lon": "-122.3493",
}


def test_parse_nominatim_reverse_response_extracts_display_name():
    assert parse_nominatim_reverse_response(NOMINATIM_REVERSE_FIXTURE, US_SUFFIX) == (
        "400 Broad St, Seattle, WA 98109"
    )


def test_parse_nominatim_reverse_response_error_key_returns_none():
    # Nominatim signals "no result" with an `error` key in an otherwise-200
    # body, not an empty body or a non-2xx status -- this must map to the
    # cacheable miss (`None`), not raise, or GeocodeWorker would retry a
    # permanent non-result forever.
    assert parse_nominatim_reverse_response({"error": "Unable to geocode"}, US_SUFFIX) is None


def test_parse_nominatim_reverse_response_missing_display_name_returns_none():
    assert parse_nominatim_reverse_response({"lat": "1.0", "lon": "2.0"}, US_SUFFIX) is None


def test_parse_nominatim_reverse_response_omit_country_empty_keeps_full_label():
    assert parse_nominatim_reverse_response(NOMINATIM_REVERSE_FIXTURE, "") == (
        "400 Broad St, Seattle, WA 98109, United States of America"
    )


def test_parse_nominatim_reverse_response_honors_a_custom_suffix():
    body = {"display_name": "10 Rue Principale, Paris, France"}
    assert parse_nominatim_reverse_response(body, "France") == "10 Rue Principale, Paris"


# ---- parse_nominatim_autocomplete_response ----

NOMINATIM_AUTOCOMPLETE_FIXTURE = [
    {
        "display_name": "400 Broad St, Seattle, WA 98109, United States of America",
        "lat": "47.6205",
        "lon": "-122.3493",
    },
    {
        "display_name": "600 4th Ave, Seattle, WA 98104, United States of America",
        "lat": "47.608",
        "lon": "-122.335",
    },
]


def test_parse_nominatim_autocomplete_response_extracts_label_lat_lon_as_floats():
    # Nominatim returns lat/lon as strings ("47.6205"), unlike Geoapify's
    # floats -- app/ui.py and the autocomplete contract both expect floats.
    results = parse_nominatim_autocomplete_response(NOMINATIM_AUTOCOMPLETE_FIXTURE, US_SUFFIX)
    assert results == [
        {"label": "400 Broad St, Seattle, WA 98109", "lat": 47.6205, "lon": -122.3493},
        {"label": "600 4th Ave, Seattle, WA 98104", "lat": 47.608, "lon": -122.335},
    ]
    for r in results:
        assert isinstance(r["lat"], float)
        assert isinstance(r["lon"], float)


def test_parse_nominatim_autocomplete_response_empty_list_returns_empty_list():
    assert parse_nominatim_autocomplete_response([], US_SUFFIX) == []


def test_parse_nominatim_autocomplete_response_drops_result_missing_label():
    body = [
        {"lat": "1.0", "lon": "2.0"},
        {"display_name": "Complete", "lat": "1.0", "lon": "2.0"},
    ]
    assert parse_nominatim_autocomplete_response(body, US_SUFFIX) == [
        {"label": "Complete", "lat": 1.0, "lon": 2.0}
    ]


def test_parse_nominatim_autocomplete_response_drops_result_with_unparseable_coordinates():
    body = [
        {"display_name": "Bad Coords", "lat": "not-a-number", "lon": "-122.0"},
        {"display_name": "Complete", "lat": "1.0", "lon": "2.0"},
    ]
    assert parse_nominatim_autocomplete_response(body, US_SUFFIX) == [
        {"label": "Complete", "lat": 1.0, "lon": 2.0}
    ]


def test_parse_nominatim_autocomplete_response_omit_country_empty_keeps_full_label():
    body = [
        {
            "display_name": "400 Broad St, Seattle, WA 98109, United States of America",
            "lat": "47.6205",
            "lon": "-122.3493",
        }
    ]
    assert parse_nominatim_autocomplete_response(body, "") == [
        {
            "label": "400 Broad St, Seattle, WA 98109, United States of America",
            "lat": 47.6205,
            "lon": -122.3493,
        }
    ]


# ---- NominatimProvider: thin I/O wrapper ----


def test_nominatim_provider_reverse_sends_the_user_agent_and_strips_the_suffix():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "Odograph/1.2.3"
        assert request.url.params["lat"] == "47.6205"
        assert request.url.params["format"] == "jsonv2"
        return httpx.Response(200, json=NOMINATIM_REVERSE_FIXTURE)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NominatimProvider(
                base_url="http://nominatim.internal", omit_country=US_SUFFIX, app_version="1.2.3"
            )
            return await provider.reverse(client, 47.6205, -122.3493)

    assert asyncio.run(scenario()) == "400 Broad St, Seattle, WA 98109"


def test_nominatim_provider_autocomplete_sends_the_query_and_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "broad st"
        assert request.url.params["limit"] == "3"
        assert request.url.params["format"] == "jsonv2"
        return httpx.Response(200, json=NOMINATIM_AUTOCOMPLETE_FIXTURE)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NominatimProvider(
                base_url="http://nominatim.internal", omit_country=US_SUFFIX, app_version="1.0"
            )
            return await provider.autocomplete(client, "broad st", limit=3)

    results = asyncio.run(scenario())
    assert results[0]["label"] == "400 Broad St, Seattle, WA 98109"
    assert isinstance(results[0]["lat"], float)


def test_nominatim_provider_name_is_nominatim():
    assert NominatimProvider(base_url="http://x", omit_country="", app_version="1.0").name == (
        "nominatim"
    )


def test_nominatim_provider_base_url_trailing_slash_stripped():
    provider = NominatimProvider(
        base_url="http://nominatim.internal/", omit_country="", app_version="1.0"
    )
    assert provider.base_url == "http://nominatim.internal"


# ---- shared provider contract: Geoapify and Nominatim must agree ----
#
# Both providers must draw the exact same line between a cacheable miss (no
# address here -- `GeocodeWorker` should write a NULL-address cache row and
# stop asking) and an error (ask again later -- leave uncached). Getting
# this backwards in a new provider would silently cache a bad API key or a
# broken endpoint as "no address found" for every coordinate it touches.


def _geoapify_reverse_body(label: str | None) -> dict:
    if label is None:
        return {"type": "FeatureCollection", "features": []}
    return {"features": [{"properties": {"formatted": label}}]}


def _geoapify_autocomplete_body(items: list[tuple[str, float, float]]) -> dict:
    return {
        "features": [
            {"properties": {"formatted": label, "lat": lat, "lon": lon}}
            for label, lat, lon in items
        ]
    }


def _geoapify_malformed_autocomplete_body() -> dict:
    return {
        "features": [
            {"properties": {"lat": 1.0, "lon": 2.0}},  # missing label
            {"properties": {"formatted": "No Coords"}},  # missing lat/lon
            {"properties": {"formatted": "Complete", "lat": 1.0, "lon": 2.0}},
        ]
    }


def _nominatim_reverse_body(label: str | None) -> dict:
    if label is None:
        return {"error": "Unable to geocode"}
    return {"display_name": label, "lat": "1.0", "lon": "2.0"}


def _nominatim_autocomplete_body(items: list[tuple[str, float, float]]) -> list:
    return [{"display_name": label, "lat": str(lat), "lon": str(lon)} for label, lat, lon in items]


def _nominatim_malformed_autocomplete_body() -> list:
    return [
        {"lat": "1.0", "lon": "2.0"},  # missing label
        {"display_name": "No Coords"},  # missing lat/lon
        {"display_name": "Complete", "lat": "1.0", "lon": "2.0"},
    ]


@dataclass(frozen=True)
class ProviderContract:
    name: str
    make_provider: Callable[[], object]
    reverse_body: Callable[[str | None], dict]
    autocomplete_body: Callable[[list[tuple[str, float, float]]], dict | list]
    malformed_autocomplete_body: Callable[[], dict | list]


PROVIDER_CONTRACTS = [
    ProviderContract(
        name="geoapify",
        make_provider=lambda: GeoapifyProvider(api_key="k", omit_country=US_SUFFIX),
        reverse_body=_geoapify_reverse_body,
        autocomplete_body=_geoapify_autocomplete_body,
        malformed_autocomplete_body=_geoapify_malformed_autocomplete_body,
    ),
    ProviderContract(
        name="nominatim",
        make_provider=lambda: NominatimProvider(
            base_url="http://nominatim.internal", omit_country=US_SUFFIX, app_version="1.0"
        ),
        reverse_body=_nominatim_reverse_body,
        autocomplete_body=_nominatim_autocomplete_body,
        malformed_autocomplete_body=_nominatim_malformed_autocomplete_body,
    ),
]


def _reverse_via(provider, handler) -> str | None:
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider.reverse(client, 47.0, -122.0)

    return asyncio.run(scenario())


def _autocomplete_via(provider, handler) -> list[dict]:
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider.autocomplete(client, "query", limit=5)

    return asyncio.run(scenario())


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_reverse_no_result_is_a_cacheable_miss_not_an_error(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=contract.reverse_body(None))

    assert _reverse_via(contract.make_provider(), handler) is None


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_autocomplete_no_result_returns_empty_list(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=contract.autocomplete_body([]))

    assert _autocomplete_via(contract.make_provider(), handler) == []


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_autocomplete_drops_malformed_results_without_surfacing_them(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=contract.malformed_autocomplete_body())

    assert _autocomplete_via(contract.make_provider(), handler) == [
        {"label": "Complete", "lat": 1.0, "lon": 2.0}
    ]


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_autocomplete_returns_lat_lon_as_floats(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=contract.autocomplete_body([("A Place", 47.5, -122.5)]))

    results = _autocomplete_via(contract.make_provider(), handler)
    assert results == [{"label": "A Place", "lat": 47.5, "lon": -122.5}]
    assert isinstance(results[0]["lat"], float)
    assert isinstance(results[0]["lon"], float)


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_reverse_transport_error_raises(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.HTTPError):
        _reverse_via(contract.make_provider(), handler)


@pytest.mark.parametrize("contract", PROVIDER_CONTRACTS, ids=[c.name for c in PROVIDER_CONTRACTS])
def test_contract_reverse_non_2xx_raises(contract):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        _reverse_via(contract.make_provider(), handler)
