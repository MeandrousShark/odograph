# Self-hosted OSRM road-snapping

OSRM turns a GPS trace with drift and gaps into a route that actually follows
roads, which sharpens the mileage this app computes for a trip. It's entirely
optional: with `OSRM_URL` empty or unset, snapping is off and the app is
otherwise fully usable. Trips still detect, list, tag, report, and export.
See [docs/privacy.md](privacy.md) for exactly what OSRM does and doesn't see,
and [docs/security.md](security.md) for the operator hardening checklist that
covers it alongside the other optional services.

This document covers picking a road extract, running
`scripts/provision_osrm.sh` against it, wiring the result into `.env`, sizing
a host for the memory-hungry step, and refreshing an extract later.

## Is this for you

Odograph computes IRS standard-mileage deductions, so it's built for a US
operator. That's the actual reason this document exists: the app ships an
example extract for the maintainer's own region, and a US operator anywhere
else needs their own state's extract to route against, or road-snapping
quietly does nothing useful for them. Nothing here technically restricts you
to a US extract (OSRM will route against whatever regional map data you
give it, wherever that is), but the deduction figures stay US IRS mileage
rates regardless of which region you snap against.

## Choosing an extract

Extracts come from [Geofabrik](https://download.geofabrik.de/), as
`<region>-latest.osm.pbf` files updated roughly daily. For a US operator, the
right size is almost always a single state: browse to
`north-america` → `us` → your state, and copy the `.osm.pbf` link, for
example, `https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf`.
Pick the smallest extract that covers everywhere you actually drive; a
whole-country extract works but costs far more RAM and disk than most
operators need, and Geofabrik also publishes smaller sub-regional extracts
for some metro areas if a full state is still more than you need.

## Provisioning

```sh
scripts/provision_osrm.sh https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf
```

This downloads the extract, then runs `osrm-extract` → `osrm-partition` →
`osrm-customize` against it inside the same pinned `osrm/osrm-backend` image
`compose.yaml`'s `osrm` service already uses, writing the result into the
`osrmdata` compose volume. It works with either `docker compose` or
`podman-compose` on `PATH` (override autodetection with `COMPOSE_CMD` if you
have both installed). Run it from the directory containing `compose.yaml`.

**Re-running it is safe, but replaces the current contents of the `osrmdata`
volume**: the previous dataset is gone as soon as the new one starts
writing, even if the new run then fails partway through. That's expected:
provisioning a different region, or refreshing the same one, is meant to be
this one command.

On success it prints the exact value to set:

```
Provisioning complete.

Set in .env, then start (or restart) the osrm service:
  OSRM_DATASET=florida-latest.osrm
  OSRM_URL=http://osrm:5000

  docker compose --profile osrm up -d osrm
```

If it fails partway through, it diagnoses the common cases (a bad or stale
extract URL, running out of disk while downloading or processing, and the
Linux OOM killer during `osrm-extract`) instead of just relaying raw
container output. Read the printed message before digging into container
logs yourself.

## Wiring it into `.env`

Set the two values the script printed:

```sh
OSRM_DATASET=florida-latest.osrm
OSRM_URL=http://osrm:5000
```

`OSRM_DATASET` ships commented out in `.env.example` on purpose: an
uncommented default would silently point a fresh install at whichever region
happened to ship as the example. Leaving it unset is exactly what makes
`compose.yaml`'s guard on that variable fire with a clear error instead of
the `osrm` service crash-looping against an empty `/data` if you start the
profile before provisioning. Then start (or restart) the service:

```sh
docker compose --profile osrm up -d osrm      # or: podman-compose --profile osrm up -d osrm
```

`OSRM_MIN_CONFIDENCE`, `OSRM_MAX_COORDS`, `SNAP_DEBOUNCE_S`, and
`SNAP_SWEEP_S` (see `.env.example`) tune snapping behavior once it's running;
none of them need changing to get started.

## Disk and RAM sizing

**`osrm-extract` is the memory-hungry step**: it holds the whole extract's
routing graph in memory while it works, well before `osrm-partition` or
`osrm-customize` run. These are rough, order-of-magnitude figures to plan a
host with, not a guarantee for any specific extract:

| Extract size (`.osm.pbf`) | Example                    | RAM to provision with | Disk headroom |
| -------------------------- | --------------------------- | ---------------------- | -------------- |
| Under ~100 MB               | A small/mid-size US state   | ~2-4 GB                | ~2 GB          |
| ~100-500 MB                 | A large US state            | ~4-8 GB                | ~5 GB          |
| Over ~500 MB                | Multiple states, a big state | 16 GB or more           | 10 GB or more  |

If your provisioning host is smaller than the extract calls for, provision on
a larger machine and copy the finished `osrmdata` volume contents to the host
that will actually run the `osrm` service. Provisioning and serving don't
have to be the same machine, and serving (`osrm-routed`) needs far less RAM
than building the dataset does. `osrm-partition` and `osrm-customize` also
use significant memory, but consistently less than `osrm-extract` for the
same input.

## Refreshing an extract

Geofabrik regenerates extracts roughly daily, but there's no requirement to
track that closely: road networks change slowly enough that refreshing
every few months, or whenever you notice snapping quality drop somewhere
you've started driving, is enough for most operators. Refreshing is the same
command as provisioning the first time: re-run `scripts/provision_osrm.sh`
with a fresh extract URL (the same region, or a different one if you've
moved), then recreate the `osrm` service so `osrm-routed` picks up the new
data: it doesn't hot-reload a dataset that changed underneath it:

```sh
scripts/provision_osrm.sh https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf
docker compose --profile osrm up -d --force-recreate osrm   # or: podman-compose --profile osrm up -d --force-recreate osrm
```

If the new extract's filename differs from the old one (a different region,
or Geofabrik renaming a region), update `OSRM_DATASET` in `.env` to match
before recreating the service: the printed value at the end of provisioning
is always the exact one to use.
