# Self-hosted OSRM road-snapping and routing

OSRM does two things. It turns GPS traces with drift and gaps into routes that
follow roads, which sharpens the mileage of detected trips. It also routes
manual trips made with a route mode (named places or map-picked points),
computing the road distance and geometry between the endpoints.

It is optional. With `OSRM_URL` empty or unset, both are off: detected trips
still assemble from GPS traces, manual trips save with a hand-entered distance,
and everything else works. See the [privacy guide](privacy.md) for what OSRM
sees and the [hardening checklist](security.md#hardening-checklist) for how it
fits with the other optional services.

## Is this for you

Odograph ships no road data, so you provision an extract covering your own
area first; without one, snapping does nothing useful. Any region works, but
the deduction figures stay US IRS mileage rates regardless of which region you
snap against.

## Choosing an extract

Extracts come from [Geofabrik](https://download.geofabrik.de/), as
`<region>-latest.osm.pbf` files updated roughly daily. For a US operator, the
right size is almost always a single state: browse to
`north-america` > `us` > your state, and copy the `.osm.pbf` link, for
example, `https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf`.
Pick the smallest extract that covers everywhere you drive. A whole-country
extract works but costs far more RAM and disk, and Geofabrik also publishes
smaller sub-regional extracts for some metro areas.

## Driving outside the extract

OSRM only knows the roads in the extract you gave it, and a trip that leaves
that area does not fail cleanly. OSRM matches whatever spans it does have road
data for and returns nothing for the rest, so a matched fragment can come back
looking like a confident, complete result.

Odograph checks every snapped route against the trip's own raw GPS distance. A
route covering less than 85% of it is treated as not describing the trip: the
trip keeps its raw GPS distance for its displayed figure, for every total, and
for the deduction; the partial snapped route is drawn beneath the raw track
rather than in place of it; and the trip page reads **Road-snap incomplete,
showing raw GPS distance**.

Mileage therefore stays correct wherever you drive; a missing region costs only
the road-following line on those trips. If that label shows up on trips through
somewhere you drive regularly, that area is outside your extract.

## Provisioning

```sh
scripts/provision_osrm.sh https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf
```

This downloads the extract, then runs `osrm-extract` > `osrm-partition` >
`osrm-customize` against it inside the same pinned `osrm/osrm-backend` image
`compose.yaml`'s `osrm` service already uses, writing the result into the
`osrmdata` compose volume. It works with either `docker compose` or
`podman-compose` on `PATH` (override autodetection with `COMPOSE_CMD` if you
have both installed). Run it from the directory containing `compose.yaml`. The
script comes with the full release checkout; the two-file installation does not
include it.

**Re-running it replaces the current contents of the `osrmdata` volume**: the
previous dataset is gone as soon as the new one starts writing, even if the new
run then fails partway through.

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
container output.

## Wiring it into `.env`

Set the two values the script printed:

```sh
OSRM_DATASET=florida-latest.osrm
OSRM_URL=http://osrm:5000
```

`OSRM_DATASET` is omitted from the baseline `.env.example` on purpose: a
default would silently point a fresh install at someone else's region. If you
start the `osrm` profile before provisioning, the service
itself catches the empty value and exits with an explanation, rather than
failing obscurely against an empty `/data`. The service is set to restart, so
the container will exit and restart repeatedly until you either provision a
dataset or stop the profile; run `docker compose logs osrm` to read the
message. The app reads `OSRM_URL` only at startup, so recreate it along with
the `osrm` service; starting only `osrm` (as the provisioning script's printed
hint does) leaves the running app without routing:

```sh
docker compose --profile osrm up -d
# podman-compose:
podman-compose --profile osrm up -d osrm
podman-compose up -d --force-recreate app
```

Compose recreates services whose environment changed, so the Docker command
recreates the app. Snapped routes and saved addresses count against each
account's storage allowance (Settings shows **Road matching and saved
addresses**). When the allowance is exhausted, route enhancement pauses and
Trip Detail shows "Route enhancement paused for storage capacity; showing raw
GPS distance".

`OSRM_MIN_CONFIDENCE`, `OSRM_MAX_COORDS`, `SNAP_DEBOUNCE_S`, and
`SNAP_SWEEP_S` tune snapping behavior once it's running; none of them need
changing to get started. See the
[configuration reference](configuration.md#road-snapping-and-manual-trip-routing)
for their defaults.

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

Refreshing is the same command as provisioning the first time: re-run
`scripts/provision_osrm.sh` with a fresh extract URL (the same region, or a
different one if you've moved), then recreate the `osrm` service so
`osrm-routed` picks up the new data: it doesn't hot-reload a dataset that
changed underneath it:

```sh
scripts/provision_osrm.sh https://download.geofabrik.de/north-america/us/florida-latest.osm.pbf
docker compose --profile osrm up -d --force-recreate osrm
```

If the new extract's filename differs from the old one (a different region,
or Geofabrik renaming a region), update `OSRM_DATASET` in `.env` to match
before recreating the service: the printed value at the end of provisioning
is always the exact one to use.

## Snapping split trips again

Trips snapped before 1.4.1 split the trace wherever two fixes were more than a
minute apart and dropped the distance between the pieces, so they show too few
miles and often **low confidence**. Queue them once for each account:

```sh
docker compose exec app python -m app.resnap --account ACCOUNT_ID
# podman-compose:
podman-compose exec app python -m app.resnap --account ACCOUNT_ID
```

`--device DEVICE_ID` limits it to one tracking device. It prints how many trips
it queued, and running it again is harmless. Each queued trip shows its raw GPS
route until the running app snaps it again, starting within `SNAP_SWEEP_S`
(5 minutes by default).
