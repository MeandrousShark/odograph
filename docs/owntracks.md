# Connecting OwnTracks

This application is fed location data by [OwnTracks](https://owntracks.org/),
a free, open-source location-tracking app for iOS and Android. OwnTracks runs
in HTTP mode, posting your phone's location straight to your own instance.
There is no third-party server in between. For the application's normal first
steps after installation, see the [Odograph usage guide](usage.md).

## App configuration

Sign in to Odograph, open **Settings > Tracking**, and create a device for
this phone. Copy the URL, username, and password shown there into OwnTracks.
The password is shown only once; if you lose it, choose **Replace password**
and update the phone. Create a separate credential for each device.

In the OwnTracks app, open Settings and configure a connection:

- **Mode:** HTTP
- **URL:** `https://your-domain/ingest` (your instance's address, with
  `/ingest` appended, for example `https://mileage.example.com/ingest`)
- **Username:** the issued username from Tracking
- **Password:** the one-time password issued with that username
- **Device ID / Tracker ID (`tid`):** a short descriptive label; it does not
  select the account or device for a newly issued credential

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

## Device identity and upgraded installations

An issued credential selects a stable device owned by your account. Its
history stays together when the phone changes `tid` or you replace its
password. Two devices can send the same `tid` without sharing points or trips.
Vehicle assignment remains a separate choice on each trip.

An upgraded installation retains its old shared login as a **Migrated shared
login**, with existing tracker labels mapped to their original device
histories. Only this compatibility adapter uses `tid` to choose among that
account's legacy streams. A previously unseen valid label creates a stream in
that same account. Changing `.env` does not change this saved login.

Before choosing **Give this device its own password** on iOS, let OwnTracks
finish uploading its queue. Changing its username or URL clears queued
locations. Conversion keeps the history already saved in Odograph and moves
the device to an issued credential. Update OwnTracks immediately: conversion
stops that device's old label from being accepted through the shared login.
Other legacy devices keep working. After converting them, **Revoke shared
login** stops all remaining uploads through the old credential. Revocation is
durable across restarts and cannot be undone by restoring old environment
values. Recorded history remains.

Replacing a device password immediately invalidates its old password. On iOS,
turn off phone networking before choosing **Replace password**, change only
the password in OwnTracks while offline, then reconnect. A password-only edit
preserves the queue; a request with the old password receives `401`, causing
OwnTracks iOS to discard that queued location. Keep the username and URL unchanged. These distinctions
come from [OwnTracks iOS connection settings](https://github.com/owntracks/ios/blob/26.2.3/OwnTracks/OwnTracks/SettingsTVC.swift#L1002-L1027)
and its [HTTP response handling](https://github.com/owntracks/ios/blob/26.2.3/OwnTracks/OwnTracks/Connection.m#L535-L568).

Verify new uploads after updating the phone. Delayed/offline queue behavior
still needs a real-device acceptance check; source inspection and the synthetic
helper below do not replace it.

## Verify your setup

First confirm your phone appears under **Settings > Device status** and that
its newest location time advances. A completed trip also needs enough points
and a quiet period for detection.

For a synthetic pipeline check, create a separate device named exactly `test`
in **Settings > Tracking**. Save its issued username and one-time password.
From the directory containing your `compose.yaml`, run the matching release's
helper, replacing the example username with the issued one:

```sh
scripts/send_test_track.sh --username odograph_ISSUED_TEST_USERNAME
```

The helper silently prompts for the issued password. It never accepts a
password argument or reads your shared `.env` credential. Automation can use
`ODOGRAPH_TRACKING_USERNAME` and `ODOGRAPH_TRACKING_SECRET` from protected
process environment. By default it posts to `http://127.0.0.1:8077`; use
`--base-url` for another instance. Only send to a trusted HTTPS address or the
local loopback listener.

Wait about 90 seconds for the detector's debounce, then look for the short
drive in the trip list. When finished, remove this dedicated test stream:

```sh
scripts/send_test_track.sh --cleanup --username odograph_ISSUED_TEST_USERNAME
```

Cleanup resolves that issued username to its account and stable device, checks
that its device name is `test`, and deletes only that stream's trips, stays,
points, raw messages, overrides, checkpoint, and credentials. Other devices
with the same label are untouched. The test credential stops working afterward.
It uses `docker compose` or `podman-compose` to access the local database;
`COMPOSE_CMD` can select the command. Cleanup does not follow `--base-url`, so
run it from the Compose directory of the instance you tested.
