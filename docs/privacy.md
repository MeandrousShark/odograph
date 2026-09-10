# Privacy and data flow

Odograph stores your location history on your server. When you view a map,
your browser requests map tiles from OpenStreetMap by default. Optional
integrations can send additional data to other services. This guide explains
each connection and what it shares.

See [security.md](security.md) for the trust model and entry-point security
of the application itself. This document is about what data leaves your
instance and to whom, not about who can reach it.

This document describes exactly which parts of your instance's location
data can leave your infrastructure, and under what configuration. It's
written for the operator running their own instance, not for an end user
of a hosted service, and this project has no hosted service.

## What stays on your instance

- **Every GPS point, trip, stay, place, and report lives in your own
  Postgres/PostGIS database.** Nothing about this data is sent anywhere
  by default.
- **Road-snapping and manual trip routing run against your own OSRM
  container, if you choose to run one.** OSRM is optional and entirely
  self-hosted. When it is configured, two categories of coordinates go to a
  container you control, on infrastructure you control, and never leave it via
  this path: GPS traces from detected trips, for road snapping to correct
  drift and compute accurate mileage; and named place coordinates or
  map-picked points from routed manual trip entry. The manual-entry
  coordinates are sent while the form is being filled in, so they reach OSRM
  even for a trip the user never saves.

## What can leave your instance, and only if you configure it

- **An OIDC identity provider** (optional sign-in). Starting a login or link
  sends the browser to the configured provider and identifies Odograph's OIDC
  client. The callback exchanges the returned authorization code and receives
  the subject and any email or display-name claims the provider supplies.
  Odograph stores the configured issuer and exact subject as the durable link,
  plus email and display name as non-authoritative metadata. It does not
  retain access, refresh, or ID tokens after the callback. Logging out of
  Odograph ends only the application session; sign out at the provider
  separately to end that session too.
- **A geocoder** (optional reverse geocoding and address autocomplete),
  selected by `GEOCODE_PROVIDER`. Whichever provider you pick receives the
  same underlying data: trip-endpoint coordinates (for reverse geocoding)
  and the text you type while searching for a place (for autocomplete).
  - **`geoapify`** sends that data to Geoapify's hosted API, along with your
    `GEOCODE_API_KEY` on every call. Geoapify authenticates each request by
    key rather than by session, so the key rides along with every lookup.
  - **`nominatim`** sends the same coordinates and search text to a
    self-hosted Nominatim instance you point `GEOCODE_NOMINATIM_URL` at,
    infrastructure you run, not a third party. No API key is involved.
    Nothing stops you from pointing this at OSM's public
    `nominatim.openstreetmap.org` instead of your own, but that instance's
    usage policy forbids the kind of bulk automated lookup this app does;
    the intended deployment is a Nominatim you operate yourself, and
    respecting that policy is your responsibility if you deviate from that.

  Results are cached in the database (`geocode_cache`), so the same
  coordinate is only ever looked up once. Repeat views of the same trip do
  not re-send its coordinates. That cache is provider-agnostic: it stores
  only `(lat, lon) -> address`, with no record of which provider produced a
  given row. **Switching `GEOCODE_PROVIDER` does not refetch anything**.
  Every already-cached address keeps whatever text the previous provider
  returned, so an instance that switches providers mid-life can end up with
  addresses of mixed provenance (some from Geoapify's formatting, some from
  Nominatim's). If you want a clean switch, clear `geocode_cache` yourself;
  the geocode worker will then repopulate it from the newly configured
  provider.
- **Map tiles: whatever `MAP_TILE_URL` points at, OpenStreetMap's public
  tile server by default.** Any page that displays a map (trip detail,
  review) fetches tiles for the area you're viewing directly from your
  browser, not through the application server. The tile server sees your
  browser's IP address and the tile coordinates it requests, which by
  nature discloses the map area being viewed, even though the application
  itself never transmits a coordinate to the tile provider. OpenStreetMap's
  tile usage policy applies for as long as `MAP_TILE_URL` points at the
  default, and requires attribution. The app already renders one via
  `MAP_TILE_ATTRIBUTION`, which likewise defaults to crediting OpenStreetMap.
  Pointing `MAP_TILE_URL` at a different tile provider makes that provider's
  own attribution and usage terms your responsibility instead.
- **ntfy** (optional push notifications). The weekly unclassified-trip
  reminder is deliberately count-only, for example, "3 unclassified trips
  in the past week." The quarterly odometer reminder additionally names
  the vehicle(s) due for a reading, using whatever label you gave that
  vehicle in Settings, for example, "log an odometer reading for Prius,
  Work Van (vehicles)." Neither notification ever contains coordinates,
  addresses, or place/route names. This is a deliberate design choice:
  push notifications commonly preview on a locked phone screen, where
  anyone glancing at the device could otherwise see location details; a
  vehicle name you chose yourself carries no location information.
- **SMTP email digests** (optional). If you configure email, digest and
  reminder messages contain aggregate figures only: total business
  miles, deduction amounts, and counts of unclassified trips. The
  quarterly odometer reminder email likewise names the vehicle(s) due for
  a reading, using your own vehicle labels. None of these messages contain
  coordinates or street addresses.

## Logging

The application deliberately raises the `httpx` HTTP client library's log
level to `WARNING`, specifically because a Geoapify request carries your API
key as a URL query parameter. At the default `INFO` level, that client
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
  coordinate it was looking up. An HTTP client's error message for a
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
  is a plain "Internal Server Error" (no traceback, no SQL, no bound
  parameter values) even when the underlying failure is a database
  constraint violation whose own error detail would otherwise echo back
  the exact row values involved (Postgres's duplicate-key error, for
  example, names the conflicting coordinate).
- **Rejected OIDC callbacks log only a fixed reason or exception type.** The
  application does not log provider subjects, tokens, or a rejected email.
  `ALLOWED_EMAIL` applies only to the one-time OIDC-only upgrade transition,
  and a rejection there uses the same generic warning.

## Summary

If you never configure a geocoder, ntfy, or SMTP, no location data leaves
your instance at all except for map-tile fetches your own browser makes
while you're looking at a map: OpenStreetMap's tile server by default, or
whatever `MAP_TILE_URL` names instead. Everything else (detection,
storage, reporting, road-snapping) stays entirely within infrastructure
you control, and a self-hosted Nominatim keeps geocoding within that same
boundary too. Configured OIDC sends authentication data to its provider, not
location history.
