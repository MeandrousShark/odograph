# Privacy and data flow

What stays on your instance, what each optional integration sends, and what
the logs leave out. For who can reach the application, see
[security.md](security.md).

## What stays on your instance

Every GPS point, trip, stay, place and report lives in your own
Postgres/PostGIS database and is sent nowhere by default. Without a geocoder,
ntfy, SMTP or OIDC, the only outside connection is your browser fetching map
tiles.

## Optional integrations

- **Map tiles.** Your browser fetches tiles directly from `MAP_TILE_URL`
  (OpenStreetMap by default). The tile server sees your IP address and the map
  area you view, plus your instance's origin as the `Referer`. Point
  `MAP_TILE_URL` at your own tile server to keep this local; its attribution
  is set with `MAP_TILE_ATTRIBUTION`.
- **OSRM** (self-hosted). Detected trips' GPS traces, for road snapping, and
  the points of a routed manual trip go to the OSRM container you run.
  Manual-trip points are sent while the form is filled in, even if the trip is
  never saved. Leave `OSRM_URL` unset to turn it off.
- **Geocoder.** Trip endpoint coordinates (reverse geocoding) and the text you
  type in address search (adding a Place, and **Search** on the manual trip
  form) go to the provider set by `GEOCODE_PROVIDER`. Typed text is sent as
  you type, before anything is saved. `geoapify` sends it to Geoapify with
  your `GEOCODE_API_KEY`; `nominatim` sends it to the Nominatim at
  `GEOCODE_NOMINATIM_URL`, with no key. OSM's public Nominatim usage policy
  forbids this kind of bulk lookup; run your own. Addresses are cached per
  account and not refetched when you change provider. Leave both
  `GEOCODE_PROVIDER` and `GEOCODE_API_KEY` unset to turn it off; **Search**
  is then hidden.
- **OIDC sign-in.** The provider sees each sign-in and returns the subject
  and any email and display name. Odograph stores the issuer, subject, email
  and display name, and keeps no access, refresh or ID tokens. An invitation
  token is never sent to the provider. Signing out of Odograph does not sign
  out of the provider. Leave the `OIDC_*` variables unset to turn it off.
- **ntfy.** The weekly reminder sends only a count of unclassified trips. The
  odometer reminder names the vehicles due, using your vehicle labels. Neither
  contains coordinates, addresses or place names. Leave `NTFY_URL` unset to
  turn it off.
- **SMTP.** Digests and reminders carry totals (business miles, deduction,
  unclassified-trip count) and vehicle labels, never coordinates or
  addresses. Invitation, password-reset and email-confirmation messages carry
  one-time links. Leave `SMTP_HOST` unset to turn it off.

Portable exports omit tracking credentials, notification delivery history and
the geocode cache. A full database backup holds everything, including
authentication state; see [backups.md](backups.md).

## Logging

- `httpx` logs at `WARNING`, so outgoing request URLs, which carry a Geoapify
  API key and coordinates or search text, are never logged.
- A failed geocoder, address search or OSRM missing-trip lookup is logged by
  exception type only, never with the API key, coordinates or search text.
- A geocoder lookup that finds no address is logged without its coordinates.
- Road snapping that cannot snap a trip logs the trip ID, not its
  coordinates.
- A rejected ingest message (oversized, unparseable, not an object, or an
  invalid location) is logged by size or a fixed reason, never its body.
- An unhandled error returns a plain `Internal Server Error` with no
  traceback, SQL or row values.
- A rejected OIDC callback logs a fixed reason or exception type, never
  provider subjects, tokens or email.
