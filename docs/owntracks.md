# Connecting OwnTracks

This application is fed location data by [OwnTracks](https://owntracks.org/),
a free, open-source location-tracking app for iOS and Android. OwnTracks runs
in HTTP mode, posting your phone's location straight to your own instance.
There is no third-party server in between. For the application's normal first
steps after installation, see the [Odograph usage guide](usage.md).

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

## Monitoring mode and battery use

OwnTracks offers a few location-reporting modes. **Move** is the most detailed
mode while driving, but it uses more battery. **Significant changes** mode uses
less power but produces sparser and less precise trip boundaries because it
reports much less often while driving. A practical setup is to use Move while
driving and let OwnTracks fall back to Significant changes while the phone is
stationary or the battery is low.

The exact controls differ between iOS and Android. OwnTracks documents the
platform behavior in its [location guide](https://owntracks.org/booklet/features/location/).

### iOS battery settings

In Move mode, `locatorInterval` is the maximum time between location publishes
and `locatorDisplacement` is the distance that can trigger one sooner. The
OwnTracks defaults are 300 seconds and 100 meters. Increasing the interval or
displacement can reduce battery use, but it also reduces the detail available
to the trip detector.

OwnTracks iOS also provides two automatic battery-saving controls:

- **`downgrade`:** the battery percentage threshold below which OwnTracks
  switches from Move to Significant changes. When the charger is connected,
  OwnTracks switches back to Move after the next Significant-mode location
  update.
- **`adapt`:** the number of minutes without movement before OwnTracks switches
  from Move to Significant changes. It switches back to Move after the next
  Significant-mode update. OwnTracks requires an active `+follow` region for
  `adapt` to take effect.

These controls are documented by OwnTracks as app-version features, not iOS
version features. Check the [OwnTracks app guide](https://owntracks.org/booklet/guide/apps/)
if a setting is missing from the installed app.

### iOS regions for common places

An OwnTracks region is a circular geofence around a place. Create one for each
common place where you want lower-power tracking, such as home, work, or a
regular parking location. Give the region a meaningful name and a radius that
covers the place without overlapping nearby places unnecessarily.

For automatic monitoring-mode changes, end the region name with
`|<mode on enter>|<mode on exit>`. The mode numbers are:

- `1`: Significant changes
- `2`: Move

For example, name a home region `Home|1|2` and a work region `Work|1|2`.
Entering either region switches from Move to Significant changes. Leaving it
switches back to Move. OwnTracks documents this syntax in its [iOS region
guide](https://owntracks.org/booklet/features/ios/).

Also create a separate region named `+follow` with an initial radius greater
than zero. OwnTracks moves this region along with the phone and adjusts its
radius as the phone moves. It is intended to wake the app and make the return
to Move mode more reliable. A `+follow` region does not produce ordinary
enter/leave events. OwnTracks also supports names such as `+60follow` to use a
60-second movement window instead of the default 30 seconds. See the [OwnTracks
waypoints guide](https://owntracks.org/booklet/features/waypoints/) for the
general region setup and the [iOS guide](https://owntracks.org/booklet/features/ios/)
for `+follow` details.

Keep the automatic mode-change regions separate from the `+follow` region. The
common-place regions control the mode; `+follow` helps OwnTracks notice movement
and wake up while the mode changes.

OwnTracks publishes region and transition messages as well as normal location
messages. Odograph stores those non-location messages in the raw ingest history
but uses only valid location messages to create points and trips, so enabling
regions does not create phantom trips.

### Android note

On Android, `locatorInterval` controls the desired interval in Significant
changes mode, `moveModeLocatorInterval` controls Move mode, and
`locatorDisplacement` can suppress Significant-mode updates until the phone has
moved far enough. `locatorDisplacement` is ignored in Move mode.

The documented `+follow` and `Home|1|2` automatic mode-change syntax is an iOS
feature. The [OwnTracks Android guide](https://owntracks.org/booklet/features/android/)
documents automation through Tasker or MacroDroid intents instead. Android
battery optimization and vendor background restrictions can also delay or stop
background reporting, so follow OwnTracks' Android background-running guidance
if updates become intermittent.

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

If the test trip never appears, open **Settings**, expand **Diagnostics**, and
find **Device status**. It shows the newest location fix received from every device
that has posted to `/ingest`, which tells you whether the test script (or a
real phone) is actually reaching your instance before you go looking any
further.
