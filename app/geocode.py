"""Reverse geocoding + address autocomplete behind a small provider
protocol, plus `GeocodeWorker` (below). Same "pure function + thin I/O
wrapper" convention as `app/snap.py`: each provider exposes pure
`parse_*_response` functions (no network, unit-testable on a decoded body)
and a small class holding only its own configuration.

Nominatim (self-hosted only) follows the same shape -- a class satisfying
the `GeocodeProvider` protocol below plus its own `parse_*_response` pair --
without changing anything in this module's Geoapify section.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx
from psycopg_pool import AsyncConnectionPool

from app.worker import PokeSweepWorker

log = logging.getLogger(__name__)

GEOCODE_PRECISION = 4  # keep in sync with migration 006's numeric(8,4)


def round_coord(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, GEOCODE_PRECISION), round(lon, GEOCODE_PRECISION))


def strip_country_suffix(label: str, suffix: str) -> str:
    """Drop a trailing ", {suffix}" from a geocoded label -- e.g. "United
    States of America" repeated on every address returned to a single-
    country deployment is pure noise. Empty `suffix` disables stripping
    entirely. Operates only on the label string a provider already
    returned, never on provider-specific metadata (Geoapify's
    `country_code`, or any other provider's equivalent), so the same
    function serves every provider unchanged.
    """
    if not suffix:
        return label
    trailing = f", {suffix}"
    return label[: -len(trailing)] if label.endswith(trailing) else label


class GeocodeProvider(Protocol):
    """`reverse`/`autocomplete` take the caller-owned httpx client rather
    than holding one themselves -- `app/main.py`'s lifespan owns the shared
    `geocode_http_client` and deliberately keeps it shared between the
    worker and `/places/search`, since its log level is configured so a
    provider's API key never reaches the logs (see `GeoapifyProvider.
    reverse`'s docstring and `app/main.py`'s httpx log-level comment).
    `name` is read by `app/diagnose.py` so a connectivity probe can report
    which provider it ran against.
    """

    name: str

    async def reverse(self, client: httpx.AsyncClient, lat: float, lon: float) -> str | None: ...

    async def autocomplete(
        self, client: httpx.AsyncClient, query: str, limit: int = 5
    ) -> list[dict]: ...


# ---- Geoapify ----

GEOAPIFY_REVERSE_URL = "https://api.geoapify.com/v1/geocode/reverse"
GEOAPIFY_AUTOCOMPLETE_URL = "https://api.geoapify.com/v1/geocode/autocomplete"


def parse_geoapify_reverse_response(body: dict, omit_country: str) -> str | None:
    """Extract a display address from Geoapify's Reverse Geocoding GeoJSON
    response. `None` if `features` is empty (matches the "cache the miss"
    policy in `GeocodeWorker`) -- a coordinate in the middle of a lake is a
    legitimate, permanent non-result, not an error.
    """
    features = body.get("features") or []
    if not features:
        return None
    props = features[0].get("properties") or {}
    formatted = props.get("formatted")
    if not formatted:
        return None
    return strip_country_suffix(formatted, omit_country)


def parse_geoapify_autocomplete_response(body: dict, omit_country: str) -> list[dict]:
    """Extract `[{"label": str, "lat": float, "lon": float}, ...]` from
    Geoapify's Autocomplete GeoJSON response, for the places-UI search.
    A feature missing any of the three fields is dropped rather than
    surfaced half-populated.
    """
    results = []
    for feature in body.get("features") or []:
        props = feature.get("properties") or {}
        label, lat, lon = props.get("formatted"), props.get("lat"), props.get("lon")
        if label is None or lat is None or lon is None:
            continue
        results.append({"label": strip_country_suffix(label, omit_country), "lat": lat, "lon": lon})
    return results


class GeoapifyProvider:
    """Thin I/O wrapper around Geoapify's Reverse Geocoding + Autocomplete
    APIs. Holds only its own configuration (API key, country-suffix
    setting) -- never an httpx client; see `GeocodeProvider`'s docstring.
    """

    name = "geoapify"

    def __init__(self, api_key: str, omit_country: str):
        self.api_key = api_key
        self.omit_country = omit_country

    async def reverse(self, client: httpx.AsyncClient, lat: float, lon: float) -> str | None:
        """Raises on transport failure or a non-2xx status (auth/rate-limit
        errors included) so the caller can distinguish "ask again later"
        from a genuine empty result -- Geoapify signals those errors via
        HTTP status, not via an empty `features` list, so
        `raise_for_status()` here (unlike OSRM's `/match` in
        `app/snap.py`, which encodes failure in a 200 body) is what keeps
        a bad API key from getting silently cached as "no address found"
        for every coordinate it touches.
        """
        resp = await client.get(
            GEOAPIFY_REVERSE_URL,
            params={"lat": lat, "lon": lon, "apiKey": self.api_key, "format": "geojson"},
        )
        resp.raise_for_status()
        return parse_geoapify_reverse_response(resp.json(), self.omit_country)

    async def autocomplete(
        self, client: httpx.AsyncClient, query: str, limit: int = 5
    ) -> list[dict]:
        resp = await client.get(
            GEOAPIFY_AUTOCOMPLETE_URL,
            params={"text": query, "apiKey": self.api_key, "format": "geojson", "limit": limit},
        )
        resp.raise_for_status()
        return parse_geoapify_autocomplete_response(resp.json(), self.omit_country)


# ---- Nominatim ----


def parse_nominatim_reverse_response(body: dict, omit_country: str) -> str | None:
    """Extract `display_name` from a Nominatim `/reverse?format=jsonv2`
    response. Nominatim signals "no result" (e.g. a coordinate over open
    water) with an `error` key in an otherwise-200 response, not an empty
    body or a non-2xx status -- that has to map to `None` here (the "cache
    the miss" policy in `GeocodeWorker`), the same as Geoapify's empty
    `features` list, or a permanent non-result would look identical to a
    malformed response and get treated as "ask again later" forever.
    """
    if "error" in body:
        return None
    display_name = body.get("display_name")
    if not display_name:
        return None
    return strip_country_suffix(display_name, omit_country)


def parse_nominatim_autocomplete_response(body: list, omit_country: str) -> list[dict]:
    """Extract `[{"label": str, "lat": float, "lon": float}, ...]` from a
    Nominatim `/search?format=jsonv2` response (a bare list of results,
    unlike Geoapify's GeoJSON `FeatureCollection`). Nominatim returns
    `lat`/`lon` as strings ("47.6062"), not floats -- `app/ui.py` and the
    autocomplete contract both expect floats, so a result whose coordinates
    won't parse is dropped rather than surfaced half-populated, same as a
    result missing its label entirely.
    """
    results = []
    for item in body or []:
        label = item.get("display_name")
        if not label:
            continue
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        results.append({"label": strip_country_suffix(label, omit_country), "lat": lat, "lon": lon})
    return results


class NominatimProvider:
    """Thin I/O wrapper around a self-hosted Nominatim's `/reverse` and
    `/search` endpoints. Holds only its own configuration -- base URL,
    country-suffix setting, the app version its User-Agent identifies --
    never an httpx client; see `GeocodeProvider`'s docstring.

    `base_url` has no shipped default (see `build_geocode_provider`): the
    public `nominatim.openstreetmap.org` instance's usage policy forbids
    the kind of bulk automated lookup this app does, so defaulting to it
    would point every operator's trip endpoints at OSM's donated servers
    without their consent. Pointing this at the public instance anyway is
    the operator's own choice and their policy responsibility to make.
    """

    name = "nominatim"

    def __init__(self, base_url: str, omit_country: str, app_version: str):
        self.base_url = base_url.rstrip("/")
        self.omit_country = omit_country
        self._headers = {"User-Agent": f"Odograph/{app_version}"}

    async def reverse(self, client: httpx.AsyncClient, lat: float, lon: float) -> str | None:
        """Raises on transport failure or a non-2xx status, so the caller
        can distinguish "ask again later" from a genuine empty result --
        the empty-result case itself arrives as a 200 body carrying an
        `error` key (handled in `parse_nominatim_reverse_response`), not as
        a status this method could catch here.
        """
        resp = await client.get(
            f"{self.base_url}/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2"},
            headers=self._headers,
        )
        resp.raise_for_status()
        return parse_nominatim_reverse_response(resp.json(), self.omit_country)

    async def autocomplete(
        self, client: httpx.AsyncClient, query: str, limit: int = 5
    ) -> list[dict]:
        resp = await client.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "jsonv2", "limit": limit},
            headers=self._headers,
        )
        resp.raise_for_status()
        return parse_nominatim_autocomplete_response(resp.json(), self.omit_country)


# ---- provider resolution ----

GEOCODE_PROVIDER_NAMES = ("geoapify", "nominatim")


def resolve_geocode_provider_name(raw_value: str, api_key: str) -> str:
    """Resolve `GEOCODE_PROVIDER` to "", "geoapify", or "nominatim" --
    called from `Config.from_env()` so an unrecognised value fails startup
    immediately rather than silently disabling geocoding.

    Unset/empty falls back to "geoapify" whenever `GEOCODE_API_KEY` is set:
    production ran with only that key configured before `GEOCODE_PROVIDER`
    existed, and this fallback is what lets an existing `.env` keep
    geocoding with zero edits after upgrading.
    """
    name = raw_value.strip().lower()
    if name:
        if name not in GEOCODE_PROVIDER_NAMES:
            raise RuntimeError(
                f"Unrecognised GEOCODE_PROVIDER={raw_value!r}; accepted values "
                f"are unset/empty (disabled), {', '.join(GEOCODE_PROVIDER_NAMES)}"
            )
        return name
    return "geoapify" if api_key else ""


def build_geocode_provider(
    name: str, *, api_key: str, omit_country: str, nominatim_url: str = "", app_version: str = "dev"
) -> GeocodeProvider | None:
    """Build the provider object `resolve_geocode_provider_name` resolved
    to. `name` is always already-validated by that function; "nominatim"
    additionally requires `nominatim_url` (`GEOCODE_NOMINATIM_URL`), which
    ships with no default (see `NominatimProvider`'s docstring) -- raising
    here rather than silently disabling geocoding keeps an operator who
    selects nominatim without a URL failing fast, the same as a genuinely
    unrecognised `GEOCODE_PROVIDER` value.
    """
    if not name:
        return None
    if name == "geoapify":
        return GeoapifyProvider(api_key=api_key, omit_country=omit_country)
    if name == "nominatim":
        if not nominatim_url:
            raise RuntimeError(
                "GEOCODE_PROVIDER=nominatim requires GEOCODE_NOMINATIM_URL to be set to "
                "your own self-hosted instance -- no default is shipped; see .env.example"
            )
        return NominatimProvider(
            base_url=nominatim_url, omit_country=omit_country, app_version=app_version
        )
    raise AssertionError(f"unreachable: unvalidated geocode provider name {name!r}")


class GeocodeWorker(PokeSweepWorker):
    """Poke+sweep background worker filling `geocode_cache` for trip
    endpoints that resolved to neither a named place nor (already) a cached
    address. Lighter than `SnapWorker`: "needs geocoding" is a pure SQL
    query over `trips` LEFT JOIN-equivalent (an `EXCEPT`) against
    `geocode_cache`, not a stored per-trip status column, so there's no
    terminal-failure enum to manage -- a cache row's existence (even with a
    NULL address) *is* the "don't retry" signal.

    The poke/debounce/sweep loop, `start`/`stop`, and guarded-run wrapper
    live in `PokeSweepWorker` (app/worker.py) -- shared with
    `DetectorScheduler` and `SnapWorker`; this class only supplies
    `run_once()`.

    Processes its batch **one coordinate at a time with a fixed delay**
    (`min_interval_s`) rather than `SnapWorker`'s all-at-once loop, since a
    provider's own rate limits are enforced server-side and this local
    pacing exists as a courtesy default (avoiding a large backfill burning
    a whole quota window in one sweep) rather than the only thing standing
    between the app and a policy violation. Each result (hit or miss) is
    written to `geocode_cache` immediately, so a crash mid-batch loses no
    progress.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        http_client: httpx.AsyncClient,
        provider: GeocodeProvider,
        min_interval_s: float,
        debounce_s: float,
        sweep_s: float,
        batch_size: int = 20,
    ):
        super().__init__(
            task_name="geocode-worker",
            log=log,
            failure_message="geocode worker run failed; will retry on next debounce/sweep",
            debounce_s=debounce_s,
            sweep_s=sweep_s,
        )
        self.pool = pool
        self.http = http_client
        self.provider = provider
        self.min_interval_s = min_interval_s
        self.batch_size = batch_size

    async def run_once(self) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT lat, lon FROM (
                    SELECT DISTINCT ROUND(ST_Y(start_geom::geometry)::numeric, {GEOCODE_PRECISION}) AS lat,
                                    ROUND(ST_X(start_geom::geometry)::numeric, {GEOCODE_PRECISION}) AS lon
                    FROM trips WHERE start_place_id IS NULL AND start_geom IS NOT NULL
                    UNION
                    SELECT DISTINCT ROUND(ST_Y(end_geom::geometry)::numeric, {GEOCODE_PRECISION}),
                                    ROUND(ST_X(end_geom::geometry)::numeric, {GEOCODE_PRECISION})
                    FROM trips WHERE end_place_id IS NULL AND end_geom IS NOT NULL
                ) endpoints
                EXCEPT
                SELECT lat, lon FROM geocode_cache
                LIMIT %s
                """,
                (self.batch_size,),
            )
            coords = [(float(r[0]), float(r[1])) for r in await cur.fetchall()]
        if not coords:
            return
        for i, (lat, lon) in enumerate(coords):
            if i > 0:
                await asyncio.sleep(self.min_interval_s)
            await self._geocode_one(lat, lon)

    async def _geocode_one(self, lat: float, lon: float) -> None:
        try:
            address = await self.provider.reverse(self.http, lat, lon)
        except (httpx.HTTPError, ValueError) as e:
            # Neither the coordinate nor str(e): a raise_for_status()
            # HTTPStatusError's message embeds the full request URL, and a
            # provider API key rides along as a query parameter on every
            # call (see app/main.py's httpx log-level note) -- the
            # exception's type alone is enough to tell "transient" from
            # "misconfigured" apart without putting the key or a precise
            # location in logs.
            log.warning("geocode: lookup failed (%s), leaving uncached", type(e).__name__)
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO geocode_cache (lat, lon, address) VALUES (%s, %s, %s) "
                "ON CONFLICT (lat, lon) DO NOTHING",
                (lat, lon, address),
            )
        if address is None:
            log.info("geocode: lookup complete, no address found")
