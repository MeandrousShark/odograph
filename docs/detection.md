# How well does detection work?

Plain answers about what trip detection does and does not do. Odograph works
from position, time and the phone app's report type. The distances and times below are the default
settings, and an operator can change them under
[Detector behavior](configuration.md#detector-behavior).

## What happens when GPS reception disappears?

A gap does not end a trip by itself. A trip ends when Odograph sees a stop:
five minutes within about 150 m, five minutes moving slower than about 4.5
mph (7.2 km/h), or about a minute of reports at the destination that include
an OwnTracks region or iOS visit report. Without one of those reports, the
trip waits for five minutes of points at the destination, which can mean the
phone's next report.

- **Lost signal while driving, such as a tunnel.** When points resume farther
  along, it stays one trip. Distance across the gap is a straight line, and
  road snapping can leave it out, so it can read short on a winding road. Gaps
  over 10 minutes get a **Recording gap** badge. Shorter ones do not.
- **Lost signal while barely moving, such as a jam in a tunnel.** If the gap
  lasts five minutes or more and averages under about 4.5 mph, it looks
  exactly like a stop. The trip splits in two, those miles are not counted,
  and there is no Recording gap badge. In a synthetic test, a 5 km drive with
  a 15-minute, 1 km gap came out as two trips totaling 3.7 km. If the two
  trips end up more than 500 m apart, as they did there, you get the Possible
  missing trip warning below.
- **Phone off or tracking stopped for a whole drive.** Odograph has no points
  from that drive, only the last one before it and the first one after. If
  tracking resumes soon enough that the jump averages over about 4.5 mph, you
  get a two-point trip measured in a straight line, with a Recording gap badge
  if the silence lasted over 10 minutes. If it resumes later, the silence looks
  like a stop and no trip is recorded. Then, if your next trip starts more than
  500 m from where the last one ended, a **Possible missing trip** warning
  offers a prefilled manual trip.
- **GPS fine, but no data connection.** Odograph accepts late points, sorts
  them by their recorded time and re-detects that stretch, keeping tags on
  trips that still match. Whether your phone stores points while offline is
  up to the app. On the maintainer's iPhone, OwnTracks has reliably uploaded
  trips after driving through areas with no service. On iOS it drops a queued
  location if the server answers 401, for example after a password change.
  Offline behavior with Traccar Client is untested.

## Does it distinguish driving from walking?

Mostly by speed, and not by travel mode. Odograph does not know whether you
are in a car.

- A steady walk usually is not a trip. Sustained movement under about 4.5 mph
  counts as not driving, and anything under about 300 m (0.2 mi) is dropped.
  GPS noise on a brisk walk can still make one.
- A bike ride, a run, a bus or a train will show up as a trip. Delete it or
  mark it **Not one of my vehicles**, and it stays out of your mileage.
- Sitting still for about five minutes or more mid-drive, at a drive-through or
  a long rail crossing, ends the trip and starts another. Merge fixes that.
  Brief stops do not split a trip.
- Stop-and-go traffic normally stays one trip. A long crawl under about 4.5
  mph can split it and lose those miles, depending on GPS noise.

## Can I correct an incorrectly detected trip?

Yes.

- Set category, vehicle, exclusion, purpose and notes.
- [Merge trips that were split, or split one that was bundled](usage.md#trip-detail),
  on back-to-back detected trips from one device. Both can be undone from
  **Settings > Manual trip edits**, and they survive later re-detection.
- Delete a wrong trip. Deleted detected trips can be restored.
- [Add a manual trip](usage.md#add-a-manual-trip) for anything missed.

You cannot edit the start time, end time or distance of a detected trip. If
those are wrong, delete it and add a manual trip. You also cannot edit
individual location points.

## How much battery does the tracking consume?

Odograph runs on your server, so the cost comes from the phone app, and it
depends on the mode.

On the maintainer's iPhone, OwnTracks regularly accounts for 2 to 4% of total
battery use, on days with about 75 miles of driving, using the region setup
below. Over two days on the same phone, Traccar Client did not appear in the
battery list after 10 miles and showed 1% after 60 miles. These are shares of
what the phone used, not of a full charge, and they are one phone rather than
a benchmark. Android has not been measured.

- **Move mode** gives the best trip detail. OwnTracks warns its battery use can
  be as high as a navigation app, so use it only while driving.
- **Significant changes** uses much less, but trips get rougher boundaries and
  distance.
- On iOS, regions switch between them for you: name a region `Home|1|2` or
  `Work|1|2`, and OwnTracks drops to Significant changes inside it and returns
  to Move when you leave. Add a `+follow` region to make the switch back more
  reliable. [Connecting OwnTracks](owntracks.md#ios-regions-for-common-places)
  has the steps.
