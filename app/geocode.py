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

from app.account_context import account_id
from app.worker import BatchOutcome
from app.provider_http import bounded_json, GEOCODE_RESPONSE_MAX_BYTES
from app.provider_pacing import ProviderPacer
from app.capacity import current_owner, owned_thread
from app.storage import enhancement_available, is_storage_capacity_error

import asyncio
import logging
from typing import Protocol

import httpx
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

GEOCODE_PRECISION = 4  # keep in sync with migration 006's numeric(8,4)
CACHED_ADDRESS_MAX_BYTES = 4_096
CAPACITY_RECHECK_S = 300.0


def _validate_cacheable_address(address: str | None) -> str | None:
    if address is not None and (
        not isinstance(address, str) or len(address.encode("utf-8")) > CACHED_ADDRESS_MAX_BYTES
    ):
        raise ValueError("provider address exceeds cached address limit")
    return address


def _parse_provider_response(parser, body, omit_country):
    try:
        return parser(body, omit_country)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("malformed geocode response") from exc


async def _parse_response(parser, body, omit_country):
    if current_owner() is not None:
        return await owned_thread(_parse_provider_response, parser, body, omit_country)
    return _parse_provider_response(parser, body, omit_country)


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
        body = await bounded_json(client, "GET",
            GEOAPIFY_REVERSE_URL,
            max_bytes=GEOCODE_RESPONSE_MAX_BYTES,
            params={"lat": lat, "lon": lon, "apiKey": self.api_key, "format": "geojson"},
        )
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise ValueError("malformed geocode response")
        if body["features"]:
            feature = body["features"][0]
            props = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(props, dict) or not isinstance(props.get("formatted"), str) or not props["formatted"]:
                raise ValueError("malformed geocode address")
        return await _parse_response(parse_geoapify_reverse_response, body, self.omit_country)

    async def autocomplete(
        self, client: httpx.AsyncClient, query: str, limit: int = 5
    ) -> list[dict]:
        body = await bounded_json(client, "GET",
            GEOAPIFY_AUTOCOMPLETE_URL,
            max_bytes=GEOCODE_RESPONSE_MAX_BYTES,
            params={"text": query, "apiKey": self.api_key, "format": "geojson", "limit": limit},
        )
        if not isinstance(body, dict) or not isinstance(body.get("features"), list):
            raise ValueError("malformed geocode response")
        return await _parse_response(parse_geoapify_autocomplete_response, body, self.omit_country)


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
    `lat`/`lon` as strings ("47.6062"), not floats -- `app/ui/places.py` and
    the autocomplete contract both expect floats, so a result whose coordinates
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
        body = await bounded_json(client, "GET",
            f"{self.base_url}/reverse",
            max_bytes=GEOCODE_RESPONSE_MAX_BYTES,
            params={"lat": lat, "lon": lon, "format": "jsonv2"},
            headers=self._headers,
        )
        if not isinstance(body, dict):
            raise ValueError("malformed geocode response")
        label = body.get("error") if "error" in body else body.get("display_name")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("malformed geocode response")
        return await _parse_response(parse_nominatim_reverse_response, body, self.omit_country)

    async def autocomplete(
        self, client: httpx.AsyncClient, query: str, limit: int = 5
    ) -> list[dict]:
        body = await bounded_json(client, "GET",
            f"{self.base_url}/search",
            max_bytes=GEOCODE_RESPONSE_MAX_BYTES,
            params={"q": query, "format": "jsonv2", "limit": limit},
            headers=self._headers,
        )
        if not isinstance(body, list):
            raise ValueError("malformed geocode response")
        return await _parse_response(parse_nominatim_autocomplete_response, body, self.omit_country)


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


class GeocodeWorker:
    """Discover bounded trip pages and complete one durable coordinate per turn."""

    def __init__(
        self, pool: AsyncConnectionPool, http_client: httpx.AsyncClient,
        provider: GeocodeProvider, min_interval_s: float, batch_size: int = 20,
        *, pacer: ProviderPacer | None = None,
    ):
        self.pool = pool
        self.http = http_client
        self.provider = provider
        self.min_interval_s = min_interval_s
        self.batch_size = batch_size
        self.pacer = pacer

    async def _coordinates(self, limit):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT rounded_lat,rounded_lon,capacity_needed_bytes FROM geocode_retry WHERE account_id=%s "
                "AND next_attempt_at<=now() ORDER BY next_attempt_at,attempted_at NULLS FIRST,"
                "rounded_lat,rounded_lon LIMIT %s", (account_id(conn), limit),
            )
            return await cur.fetchall()

    async def _progress(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT generation>scanned_generation OR cursor_trip_id>0,last_unit FROM geocode_discovery "
                "WHERE account_id=%s", (account_id(conn),),
            )
            state = await cur.fetchone()
            cur = await conn.execute(
                "SELECT next_attempt_at<=now(),GREATEST(0,EXTRACT(EPOCH FROM next_attempt_at-now())) "
                "FROM geocode_retry WHERE account_id=%s "
                "ORDER BY next_attempt_at,attempted_at NULLS FIRST,rounded_lat,rounded_lon LIMIT 1",
                (account_id(conn),),
            )
            next_row = await cur.fetchone()
        return state, next_row

    async def _discover(self):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT * FROM geocode_discover_page(%s)", (account_id(conn),))
            cur = await conn.execute(
                "SELECT capacity_paused FROM geocode_discovery WHERE account_id=%s",
                (account_id(conn),),
            )
            return (await cur.fetchone())[0]

    async def _capacity_paused(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT geocode_refresh_capacity_pause(%s)", (account_id(conn),),
            )
            return (await cur.fetchone())[0]

    async def run_turn(self, cursor=None):
        from app.worker import TurnOutcome
        state, next_retry = await self._progress()
        pending = state is not None and state[0]
        due = next_retry is not None and next_retry[0]
        if pending and (state[1] != 'discovery' or not due):
            if await self._capacity_paused():
                return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + CAPACITY_RECHECK_S)
            if await self._discover():
                return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + CAPACITY_RECHECK_S)
            return TurnOutcome(ready=True)
        if due:
            coords = await self._coordinates(1)
            if coords:
                lat, lon, needed_bytes = coords[0]
                outcome = await self._geocode_one(lat, lon, ticket=cursor)
                if outcome is None:
                    return TurnOutcome(deferred_until=self.pacer.next_start)
                if not outcome.attempted and not outcome.completed:
                    if await self._capacity_paused():
                        return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + CAPACITY_RECHECK_S)
                    async with self.pool.connection() as conn:
                        ready = await enhancement_available(
                            conn, needed_bytes=max(128, needed_bytes), account_credit_bytes=128,
                        )
                    if not ready:
                        return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + CAPACITY_RECHECK_S)
                return TurnOutcome(batch=outcome, ready=True)
            return TurnOutcome(ready=True)
        if next_retry is not None:
            return TurnOutcome(deferred_until=asyncio.get_running_loop().time() + float(next_retry[1]))
        return TurnOutcome()

    async def run_once(self) -> BatchOutcome:
        """Compatibility batch entry point; production rotates every atomic unit."""
        state, _ = await self._progress()
        if state is not None and state[0]:
            if await self._capacity_paused() or await self._discover():
                return BatchOutcome()
        coords = await self._coordinates(self.batch_size)
        outcome = BatchOutcome()
        for i, (lat, lon, _) in enumerate(coords):
            if i > 0:
                await asyncio.sleep(self.min_interval_s)
            item = await self._geocode_one(lat, lon)
            if item is not None:
                outcome += item
        return outcome

    async def _source(self, conn, lat, lon):
        cur = await conn.execute(
            "SELECT * FROM geocode_representative_source(%s,%s::numeric,%s::numeric)",
            (account_id(conn), lat, lon),
        )
        return await cur.fetchone()

    async def _queue_row(self, conn, lat, lon, *, lock=False):
        cur = await conn.execute(
            "SELECT attempted_at,next_attempt_at,failure_count,capacity_needed_bytes FROM geocode_retry "
            "WHERE account_id=%s AND rounded_lat=%s::numeric AND rounded_lon=%s::numeric"
            + (" FOR UPDATE" if lock else " AND next_attempt_at<=now()"),
            (account_id(conn), lat, lon),
        )
        return await cur.fetchone()

    async def _delete(self, conn, lat, lon):
        await conn.execute(
            "DELETE FROM geocode_retry WHERE account_id=%s AND rounded_lat=%s::numeric AND rounded_lon=%s::numeric",
            (account_id(conn), lat, lon),
        )

    async def _retry(self, conn, lat, lon, count, reason):
        count = min(31, count + 1)
        delay = min(3600, 60 * 2 ** min(count - 1, 6))
        await conn.execute(
            "UPDATE geocode_retry SET attempted_at=now(),next_attempt_at=now()+%s*interval '1 second',"
            "failure_count=%s,failure_reason=%s,capacity_paused=false,capacity_needed_bytes=0 "
            "WHERE account_id=%s AND rounded_lat=%s::numeric AND rounded_lon=%s::numeric",
            (delay, count, reason, account_id(conn), lat, lon),
        )

    async def _pause_capacity_retry(self, lat, lon, queued, needed_bytes):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT geocode_record_capacity_pause(%s)", (account_id(conn),))
            if await self._queue_row(conn, lat, lon, lock=True) != queued:
                return
            await conn.execute(
                "UPDATE geocode_retry SET attempted_at=now(),"
                "next_attempt_at=now()+%s*interval '1 second',capacity_paused=true,"
                "capacity_needed_bytes=%s WHERE account_id=%s "
                "AND rounded_lat=%s::numeric AND rounded_lon=%s::numeric",
                (CAPACITY_RECHECK_S, needed_bytes, account_id(conn), lat, lon),
            )

    async def _valid_source(self, conn, source, lat, lon):
        trip_id, generation, device_id, device_generation, eligibility_generation, side = source
        if device_id is not None:
            cur = await conn.execute(
                "SELECT 1 FROM tracking_devices WHERE account_id=%s AND id=%s AND generation=%s "
                "AND geocode_generation=%s AND enabled AND revoked_at IS NULL FOR SHARE",
                (account_id(conn), device_id, device_generation, eligibility_generation),
            )
            if await cur.fetchone() is None:
                return False
        cur = await conn.execute(
            f"SELECT 1 FROM trips WHERE account_id=%s AND id=%s AND geocode_generation=%s "
            f"AND tracking_device_id IS NOT DISTINCT FROM %s AND {side}_place_id IS NULL "
            f"AND ROUND(ST_Y({side}_geom::geometry)::numeric,4)=%s::numeric "
            f"AND ROUND(ST_X({side}_geom::geometry)::numeric,4)=%s::numeric FOR SHARE",
            (account_id(conn), trip_id, generation, device_id, lat, lon),
        )
        return await cur.fetchone() is not None

    async def _geocode_one(self, lat: float, lon: float, *, ticket=None) -> BatchOutcome | None:
        async with self.pool.connection() as conn:
            queued = await self._queue_row(conn, lat, lon)
            if queued is None:
                return BatchOutcome()
            source = await self._source(conn, lat, lon)
            cur = await conn.execute(
                "SELECT 1 FROM geocode_cache WHERE account_id=%s AND lat=%s::numeric AND lon=%s::numeric",
                (account_id(conn), lat, lon),
            )
            cached = await cur.fetchone() is not None
            can_cache = (
                not (source is not None and not cached)
                or await enhancement_available(
                    conn, needed_bytes=max(128, queued[3]), account_credit_bytes=128,
                )
            )
        attempted = source is not None and not cached
        if attempted and not can_cache:
            await self._pause_capacity_retry(lat, lon, queued, max(128, queued[3]))
            return BatchOutcome()
        if attempted and self.pacer is not None:
            if ticket is None or not self.pacer.try_start(ticket):
                return None
        failure = None
        address = None
        if attempted:
            try:
                address = _validate_cacheable_address(
                    await self.provider.reverse(self.http, float(lat), float(lon))
                )
            except (httpx.HTTPError, ValueError) as exc:
                failure = exc
                reason = ('parse' if isinstance(exc, ValueError) else
                          'http' if isinstance(exc, httpx.HTTPStatusError) else 'transport')
                log.warning("geocode: lookup failed (%s), leaving uncached", type(exc).__name__)
        try:
            async with self.pool.connection() as conn:
                # All personal writes acquire usage first. Take that same lock
                # before device/trip validation to avoid reversing writer order.
                await conn.execute("SELECT geocode_record_coordinate_turn(%s)", (account_id(conn),))
                current_queue = await self._queue_row(conn, lat, lon, lock=True)
                if current_queue != queued:
                    return BatchOutcome(attempted=int(attempted))
                if not attempted:
                    # A new source may have appeared while this turn was outside
                    # its transaction. Keep its intent instead of losing that work.
                    if cached or await self._source(conn, lat, lon) is None:
                        await self._delete(conn, lat, lon)
                        return BatchOutcome(completed=1)
                    return BatchOutcome()
                if not await self._valid_source(conn, source, lat, lon):
                    if await self._source(conn, lat, lon) is None:
                        await self._delete(conn, lat, lon)
                        return BatchOutcome(attempted=1, completed=1)
                    await self._retry(conn, lat, lon, queued[2], 'source_changed')
                    return BatchOutcome(attempted=1)
                if failure is not None:
                    await self._retry(conn, lat, lon, queued[2], reason)
                    return BatchOutcome(attempted=1, retriable_failures=1, failure_type=type(failure).__name__)
                await conn.execute(
                    "INSERT INTO geocode_cache(account_id,lat,lon,address) VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT(account_id,lat,lon) DO NOTHING", (account_id(conn), lat, lon, address),
                )
                await self._delete(conn, lat, lon)
        except Exception as exc:
            if not is_storage_capacity_error(exc):
                raise
            await self._pause_capacity_retry(
                lat, lon, queued, 128 + (len(address.encode("utf-8")) if address else 0),
            )
            return BatchOutcome(attempted=int(attempted))
        if address is None:
            log.info("geocode: lookup complete, no address found")
        return BatchOutcome(attempted=1, completed=1)
