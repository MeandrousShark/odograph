# Privacy and data flow

See [security.md](security.md) for the trust model and entry-point security
of the application itself — this document is about what data leaves your
instance and to whom, not about who can reach it.

This document describes exactly which parts of your instance's location
data can leave your infrastructure, and under what configuration. It's
written for the operator running their own instance, not for an end user
of a hosted service — this project has no hosted service.

## What stays on your instance

- **Every GPS point, trip, stay, place, and report lives in your own
  Postgres/PostGIS database.** Nothing about this data is sent anywhere
  by default.
- **Road-snapping and routing run against your own OSRM container, if you
  choose to run one.** OSRM is optional and entirely self-hosted: your
  coordinates are sent to a container you control, on infrastructure you
  control, and never leave it via this path.

## What can leave your instance — and only if you configure it

- **A geocoder** (optional reverse geocoding and address autocomplete),
  selected by `GEOCODE_PROVIDER`. Whichever provider you pick receives the
  same underlying data: trip-endpoint coordinates (for reverse geocoding)
  and the text you type while searching for a place (for autocomplete).
  - **`geoapify`** sends that data to Geoapify's hosted API, along with your
    `GEOCODE_API_KEY` on every call — Geoapify authenticates each request by
    key rather than by session, so the key rides along with every lookup.
  - **`nominatim`** sends the same coordinates and search text to a
    self-hosted Nominatim instance you point `GEOCODE_NOMINATIM_URL` at —
    infrastructure you run, not a third party. No API key is involved.
    Nothing stops you from pointing this at OSM's public
    `nominatim.openstreetmap.org` instead of your own, but that instance's
    usage policy forbids the kind of bulk automated lookup this app does;
    the intended deployment is a Nominatim you operate yourself, and
    respecting that policy is your responsibility if you deviate from that.

  Results are cached in the database (`geocode_cache`), so the same
  coordinate is only ever looked up once — repeat views of the same trip do
  not re-send its coordinates. That cache is provider-agnostic: it stores
  only `(lat, lon) -> address`, with no record of which provider produced a
  given row. **Switching `GEOCODE_PROVIDER` does not refetch anything** —
  every already-cached address keeps whatever text the previous provider
  returned, so an instance that switches providers mid-life can end up with
  addresses of mixed provenance (some from Geoapify's formatting, some from
  Nominatim's). If you want a clean switch, clear `geocode_cache` yourself;
  the geocode worker will then repopulate it from the newly configured
  provider.
- **Map tiles — whatever `MAP_TILE_URL` points at, OpenStreetMap's public
  tile server by default.** Any page that displays a map (trip detail,
  review) fetches tiles for the area you're viewing directly from your
  browser, not through the application server. The tile server sees your
  browser's IP address and the tile coordinates it requests, which by
  nature discloses the map area being viewed, even though the application
  itself never transmits a coordinate to the tile provider. OpenStreetMap's
  tile usage policy applies for as long as `MAP_TILE_URL` points at the
  default, and requires attribution — the app already renders one via
  `MAP_TILE_ATTRIBUTION`, which likewise defaults to crediting OpenStreetMap.
  Pointing `MAP_TILE_URL` at a different tile provider makes that provider's
  own attribution and usage terms your responsibility instead.
- **ntfy** (optional push notifications). The weekly unclassified-trip
  reminder is deliberately count-only — for example, "3 unclassified trips
  in the past week." The quarterly odometer reminder additionally names
  the vehicle(s) due for a reading, using whatever label you gave that
  vehicle in Settings — for example, "log an odometer reading for Prius,
  Work Van (vehicles)." Neither notification ever contains coordinates,
  addresses, or place/route names. This is a deliberate design choice:
  push notifications commonly preview on a locked phone screen, where
  anyone glancing at the device could otherwise see location details; a
  vehicle name you chose yourself carries no location information.
- **SMTP email digests** (optional). If you configure email, digest and
  reminder messages contain aggregate figures only — total business
  miles, deduction amounts, and counts of unclassified trips. The
  quarterly odometer reminder email likewise names the vehicle(s) due for
  a reading, using your own vehicle labels. None of these messages contain
  coordinates or street addresses.

## Logging

The application deliberately raises the `httpx` HTTP client library's log
level to `WARNING`, specifically because a Geoapify request carries your API
key as a URL query parameter — at the default `INFO` level, that client
logs every outgoing request URL, which would otherwise put your API key in
your application logs. A self-hosted Nominatim carries no API key, but the
same raised log level still keeps every geocoder's request URL, and the
coordinates or search text in it, out of the logs. Application-level logs
record counts and trip IDs for operational visibility, not coordinates or
addresses.

An audit of every logging call and error path in the application backs
this claim with tests, not just the statement above:

- **Failed geocode/OSRM requests never log the request itself.** A failed
  geocoder (either provider) or OSRM call is logged by exception *type*
  only (e.g. `HTTPStatusError`), never the exception's message or the
  coordinate it was looking up — an HTTP client's error message for a
  non-2xx response conventionally embeds the full request URL, and that URL
  is exactly where a Geoapify API key or a precise trip-endpoint coordinate
  lives. This covers the geocode worker, the places-search address
  autocomplete, and the missing-trip road-distance suggestion.
- **A rejected or malformed ingest submission never logs its payload.**
  An oversized, unparseable, non-object, or otherwise-rejected OwnTracks
  message is logged by size or by a fixed rejection reason (e.g.
  "lat/lon out of range"), never by echoing any part of the message body
  itself.
- **An unhandled server error returns a generic page, nothing more.** If
  something fails in a way the application didn't anticipate, the response
  is a plain "Internal Server Error" — no traceback, no SQL, no bound
  parameter values — even when the underlying failure is a database
  constraint violation whose own error detail would otherwise echo back
  the exact row values involved (Postgres's duplicate-key error, for
  example, names the conflicting coordinate).
- **One deliberate exception:** a rejected OIDC sign-in attempt (an email
  that doesn't match `ALLOWED_EMAIL`) logs that email address as a
  warning. This is a knowing tradeoff, not an oversight — diagnosing a
  misconfigured `ALLOWED_EMAIL` (a typo, a stale value, the wrong account
  signing in) without seeing which email was rejected is guesswork, and an
  email address is nowhere near as sensitive as a coordinate, an address,
  or a credential. Everything else on this page — API keys, session
  material, ingest credentials, `ADMIN_TOKEN`, the session secret, and
  every coordinate/address path above — never appears in a log line.

## Summary

If you never configure a geocoder, ntfy, or SMTP, no location data leaves
your instance at all except for map-tile fetches your own browser makes
while you're looking at a map — OpenStreetMap's tile server by default, or
whatever `MAP_TILE_URL` names instead. Everything else — detection,
storage, reporting, road-snapping — stays entirely within infrastructure
you control, and a self-hosted Nominatim keeps geocoding within that same
boundary too.
