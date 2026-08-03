# Third-Party Notices

This project is licensed under AGPL-3.0 (see [LICENSE](LICENSE)). It also
uses the following third-party software and services. Licenses below were
verified against each installed package's own metadata (`pip show
<package> | grep -i license`, cross-checked against the bundled license
file inside the package where the metadata field was empty), not merely
copied from this document's original draft.

## Python dependencies (direct)

These are the direct runtime dependencies listed in `requirements.txt`.
Transitive dependencies (pulled in by these) are not separately listed
here; see `requirements.lock` for the full resolved set.

| Package | License | Notes |
| --- | --- | --- |
| FastAPI | MIT | Verified via package metadata (`License-Expression: MIT`). |
| Starlette | BSD-3-Clause | Verified via package metadata (`License-Expression: BSD-3-Clause`). FastAPI's underlying ASGI toolkit. |
| Uvicorn | BSD-3-Clause | Verified via package metadata (`License-Expression: BSD-3-Clause`). Installed with the `[standard]` extras. |
| Jinja2 | BSD-3-Clause | Package metadata only carries a generic "BSD License" classifier; confirmed the specific variant (3-clause, copyright Pallets) against the license text bundled in the installed package's dist-info. |
| psycopg | LGPL-3.0-only | Verified via package metadata (`License-Expression: LGPL-3.0-only`). |
| psycopg-pool | LGPL-3.0-only | Verified via package metadata (`License-Expression: LGPL-3.0-only`). The Python import name is `psycopg_pool`. |
| Authlib | BSD-3-Clause | Verified via package metadata (`License: BSD-3-Clause`). |
| httpx | BSD-3-Clause | Verified via package metadata (`License: BSD-3-Clause`). |
| itsdangerous | BSD-3-Clause | Package metadata only carries a generic "BSD License" classifier; confirmed the specific variant (3-clause, copyright Pallets) against the license text bundled in the installed package's dist-info. |
| python-multipart | Apache-2.0 | Verified via package metadata (`License-Expression: Apache-2.0`). |
| openpyxl | MIT | Verified via package metadata (`License: MIT`). |

## Vendored frontend assets (`static/vendor/`)

| Asset | Version | License | In-tree license text |
| --- | --- | --- | --- |
| Leaflet | 1.9.4 (from the file's own `@preserve` header comment) | BSD-2-Clause | `static/vendor/leaflet/LICENSE` (added; the vendored build did not ship its own license file, so the upstream text was copied in alongside it) |
| htmx | 1.9.1 (from the file's embedded `version` string) | BSD-2-Clause | `static/vendor/htmx/LICENSE` (added; the vendored build did not ship its own license file, so the upstream text was copied in alongside it) |

## Services (not bundled, used at runtime if configured)

- **OpenStreetMap**: trip and route maps render OpenStreetMap tiles and
  data, which are © OpenStreetMap contributors and licensed under the Open
  Database License (ODbL). This carries an attribution requirement, which
  the app already satisfies: pages that load OSM tiles render an "©
  OpenStreetMap" attribution link on the map. See
  [openstreetmap.org/copyright](https://www.openstreetmap.org/copyright).
- **OSRM**: optional, self-hosted road-snapping and routing. OSRM
  (Open Source Routing Machine) is BSD-2-Clause licensed. This project
  does not vendor or redistribute OSRM; it talks to an instance the
  operator runs themselves.
- **Geoapify**: one of two optional reverse-geocoding/address-autocomplete
  providers, used when `GEOCODE_PROVIDER=geoapify`. This is a third-party
  paid API service, not bundled software. Operators who enable it bring
  their own API key and are responsible for accepting Geoapify's own terms
  of service.
- **Nominatim**: the other optional geocoding provider, used when
  `GEOCODE_PROVIDER=nominatim`. Unlike Geoapify, this isn't a third-party
  runtime dependency: it's self-hosted only, so it's infrastructure the
  operator runs themselves, not a service or terms of use anyone else
  controls. It still queries OSM-derived data, which carries the same ODbL
  attribution requirement as the map tiles above regardless of who's
  hosting the Nominatim instance. An operator running their own is
  responsible for that attribution the same way this project already is
  for its own use of OSM data.
