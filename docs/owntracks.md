# Connecting OwnTracks

This application is fed location data by [OwnTracks](https://owntracks.org/),
a free, open-source location-tracking app for iOS and Android. OwnTracks runs
in HTTP mode, posting your phone's location straight to your own instance.
There is no third-party server in between.

## App configuration

In the OwnTracks app, open Settings and configure a connection:

- **Mode:** HTTP
- **URL:** `https://your-domain/ingest` (your instance's address, with
  `/ingest` appended, for example `https://mileage.example.com/ingest`)
- **Username:** `owntracks`, unless you have set `INGEST_USERNAME` in `.env`
  to something else
- **Password:** the value of `INGEST_PASSWORD` from your instance's `.env`
  file
- **Device ID / Tracker ID (`tid`):** any short identifier, for example the
  first two letters of the phone's name

Leave "Auth" enabled. This is what makes OwnTracks send the username and
password above as HTTP Basic auth on every request.

## Monitoring mode

OwnTracks offers a few location-reporting modes. **Move** is recommended: it
reports frequently while you're moving and drops to occasional, low-power
fixes while stationary. This gives the trip detector enough points to find
accurate trip boundaries without draining the battery. "Significant changes"
mode also works, but produces sparser, less precise trip boundaries because
it reports far less often while driving.

## The device ID becomes your device's identity

Whatever you set as the tracker ID (`tid`) becomes this device's identity in
the application: trips, stays, and detection history are all tracked per
device. If you change a phone's `tid` later, the application treats it as a
brand-new device: existing history stays associated with the old id, and
detection for the new id starts from a clean slate. Pick a `tid` you're happy
keeping and avoid changing it on a phone that's already sending data.

If you track more than one phone (for example, two vehicles or two drivers),
give each one a distinct `tid`.

## Verify your setup

Once OwnTracks is configured, you can prove the whole pipeline (ingest,
storage, trip detection, and the UI) works end to end without waiting to
actually drive anywhere. The repository includes
`scripts/send_test_track.sh`, which posts a short synthetic stay-drive-stay
track under a fixed test device id.

From the directory containing your `compose.yaml`:

```sh
scripts/send_test_track.sh \
  --password "$(sed -n 's/^INGEST_PASSWORD=//p' .env)"
```

By default this targets `http://127.0.0.1:8077`; pass `--base-url` to point
it elsewhere. On success it prints how many points it sent.

Wait about 90 seconds, long enough for the trip detector's debounce to run,
then open the trip list in the UI. You should see one new trip on a device
named `test`, with a short drive between two stays.

Once you've confirmed it worked, remove the test data:

```sh
scripts/send_test_track.sh --cleanup
```

This deletes every trace of the `test` device (its trips, stays, points,
and raw ingest messages), leaving your real data untouched. It shells out to
`docker compose` or `podman-compose` (whichever is on your `PATH`; set
`COMPOSE_CMD` to override) to run the deletion against the database
container.

If the test trip never appears, check the "Device status" section on the
Settings page: it shows the newest location fix received from every device
that has posted to `/ingest`, which tells you whether the test script (or a
real phone) is actually reaching your instance before you go looking any
further.
