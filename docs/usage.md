# Using Odograph

What each screen is for and the parts that are not obvious. For installation
see the [README](../README.md); to connect a phone, see
[Connecting OwnTracks](owntracks.md) or [Connecting Traccar Client](traccar.md).
Each account has its own devices, vehicles, trips and settings; passwords,
sign-in and member accounts are in [Account management](accounts.md).

The main pages are **Dashboard**, **Trips**, **Review**, **Report**,
**Expenses** and **Stats**. The header links to **Settings** and, through
your name, **Account Settings**. On a phone, **Week** is the Dashboard,
**More** holds Expenses and Stats, and the avatar menu holds the rest. The
header shows your **Display name** (set in **Account Settings > Profile**,
up to 100 characters), or the part of your login email before the `@`. It is
only a label, not a login name.

## Categories and exclusions

Every trip has a category and, separately, an exclusion. The category says
what the trip was; the exclusion says whether it counts in mileage at all.

- **Business**, **Personal**, or unclassified (shown as **Unset** in edit
  controls and **Needs category** in lists). Only Business miles are priced
  for the deduction. Unclassified trips wait in Review.
- **Not one of my vehicles** removes the trip from every total, trip count,
  report, Stats figure, odometer reconciliation and missing-trip warning.
- **My vehicle, someone else drove** keeps the miles in total and vehicle
  mileage as non-deductible, outside business miles and the deduction. It is
  kept apart from Personal so another driver's miles are not counted as
  yours.

Clearing an exclusion makes it a normal trip again and keeps its category.

## Dashboard

A Monday-to-Sunday week: the total by category, daily bars, trip count,
estimated deduction and recorded expenses. **Needs attention** links to
unclassified trips and possible missing trips.

## Review

Review shows unclassified trips one at a time. Purpose, vehicle, exclusion
and notes can be edited on the card. **Next** saves the chosen Business or
Personal category with every visible field and advances; it stays disabled
until a category is chosen. **Skip** saves the other fields and advances
without a category. **Undo Last** takes back the last Next (clearing the
category it set) or Skip (field edits stay); it is forgotten on reload.

## Trips

The archive, grouped by month. Search matches notes, purposes, place names,
location names and addresses. Filters cover category, date, vehicle (or
**Unassigned**) and exclusion, and the **Export** menu downloads the trips
they match as CSV or XLSX.

**Select all matching** selects every trip the filters match, including
unloaded pages and other months. It is a snapshot: trips that arrive later
are not added. The selection survives **Load more** and bulk updates, and is
cleared by changing filters. The selection bar sets category, purpose,
vehicle or exclusion, clears the category, merges, or deletes. If any
selected trip no longer exists, the whole bulk action fails and the
selection stays.

Deleting a detected trip keeps its location points, and it can be restored
from **Settings > Manual trip edits**. A manual or imported trip is deleted
permanently. Expenses linked to a deleted trip are kept, unlinked.

### Trip Detail

The trip's map, times, point count and distance, with category, vehicle,
exclusion, purpose and notes that save as you change them. A detected trip
shows its road-snapped distance beside the raw GPS distance, or says why it
shows only the raw distance: **Not yet road-snapped**, **Road-snap failed**,
**Road-snap incomplete**, or route enhancement paused for storage. A
low-confidence snap is labelled.

**Advanced trip tools** (detected trips only, not imported ones) holds:

- **Merge** with the previous or next detected trip from the same device,
  choosing which category, purpose, notes and vehicle to keep.
- **Split**: click the route where the first trip ended. Odograph splits at
  the nearest recorded point. Both halves must be at least the detector's
  minimum trip distance
  ([`MIN_TRIP_DISTANCE_M`](configuration.md#detector-behavior)).
- **Name start** and **Name end** save an unnamed endpoint as a Place.

The archive's **Merge selected...** takes two or more back-to-back detected
trips from one device, all shown in the current view. A merge or split
re-detects the device's nearby trips, so their boundaries and distances can
change. **Settings > Manual trip edits** undoes a merge or split and restores
deleted detected trips; these edits are reapplied whenever trips are
re-detected.

A trip that has just ended can still grow if the drive continues. Until the
stop is confirmed or a later trip exists, delete, merge and split refuse it
with "This trip is still in progress. Try again once it ends." After 24 hours
with nothing from the phone, it can be edited.

**Recording gap** (distance may under-read) and **Possible missing trip**
appear on list rows. **Acknowledge warnings** in the row's **More trip
actions** menu hides them from lists and from **Needs attention**; it changes
nothing else, and Trip Detail keeps showing them with **Show in lists
again**. An acknowledgement lasts while the trip keeps its start and end
times (a gap) or its start time and the same previous trip with the same end
time (a missing trip). A merge, split or boundary change brings the warning
back. Portable exports and imports leave acknowledgements out.

### Add a manual trip

**Add manual trip** is on the Dashboard and Trips. Times use your timezone,
and an end time at or before the start time is an overnight trip. The route
mode decides where the distance comes from:

- **No route:** enter the distance, and optional start and end location
  names. These are labels for this trip only, not Places.
- **Named places**, **Pick on map** or **Search** (an address, shown only
  with a geocoder): when routing is configured, the road route is previewed
  and fills the distance, which you can overwrite.

If routing is unavailable when you save, the trip is saved with the distance
you entered and without the route, keeping any named places, and Trips says
so. With no distance and no route it is refused. Routing is the optional
[OSRM service](osrm.md). What a routed or searched trip sends to OSRM or the
geocoder, even if never saved, is in the [privacy guide](privacy.md).

For a **Possible missing trip**, **Add missing trip** in the row's menu (or
the Dashboard link) prefills the date, start time and notes, and with
routing, a suggested road distance. Check every field before saving.

## Settings

**Places** name trip endpoints: a trip end within a place's radius (meters)
is at that place. With a geocoder, address search fills the coordinates.

**Auto-tag rules** set Business or Personal from a trip's two ends. Each side
matches a place kind (Home, Work, Other), a specific place, or any end, in
either direction. When several rules match, the most specific wins, then the
oldest. A category or purpose you set or clear by hand always wins over rules
and survives re-detection. A trip a rule tagged returns to unclassified when no
rule matches it any more.

**Vehicles** appear in trip pickers, reports and expenses. **Deactivate**
removes one from pickers but keeps it on existing trips. **Assign my default
vehicle to newly detected trips** fills the vehicle automatically.

**Mileage rates** hold the IRS rate for each year, with an optional
mid-year change. See [US deduction limits](#us-deduction-limits).

**Settings > Tracking** creates a device for each phone and shows its password
once; see
[Device identity](owntracks.md#device-identity-and-upgraded-installations).
**Replace password** invalidates the old one and keeps the history;
**Revoke access** stops the device's uploads. **Device status** shows when
each device last sent a point and how many it has sent.

**Preferences** set your timezone, which dates, reports, manual entries and
reminder hours use, and this browser's theme. **Notifications** choose the
ntfy topic, email recipients, reminders and their hours. Delivery also needs
the operator's [ntfy](configuration.md#ntfy) or [SMTP](configuration.md#email)
settings.

**Diagnostics**, for administrators only, shows the app, schema and detector
versions, database, migration and worker state, and which settings are
configured. Connectivity to OSRM, the geocoder, ntfy and SMTP is checked only
when you choose **Check now**.

## Expenses

Expenses are recorded by vehicle, date, category and amount. The tax
treatment is **Business-use allocated** (scaled by business share) or
**Fully business**; Parking and Tolls default to Fully business, and Other
needs a choice. An expense can be linked to the trip that caused it; Odograph
warns when the linked trip's vehicle or local date differs, or the trip is
**Not one of my vehicles**, and changes neither record.

## Odometer readings

In **Settings > Odometer readings**, add readings for a vehicle now and then.
Between two readings, Odograph compares the odometer distance with detected
mileage and shows coverage and unaccounted miles, which reveal driving the
tracker missed. The annual report shows the same coverage, and the
actual-expense comparison uses odometer miles as total miles when it has
them. Readings never change the standard mileage deduction. To correct one,
delete it and add a new one.

## Report and Stats

**Report** opens on the previous year until the end of April, then the
current year. It shows business, personal and non-deductible miles, business
share, the deduction at your saved rates, and monthly and vehicle totals,
with notes to review before filing. Q1 to Q4 and a custom From and To open a
range report with the same totals. The annual report adds odometer coverage,
a standard versus actual expense estimate when the records exist, and a link
to the **Expense ledger**.

**Stats** shows driving for a year or date range and a vehicle: weekly and
monthly charts, category share, top saved-place routes, most-used places and
a per-vehicle breakdown.

### Report workbooks

**Export XLSX** on a report downloads a workbook:

- **Summary:** business, personal, non-deductible and total miles, business
  share, the rates applied, the total deduction and trip count, a
  month-by-month table (trips, business miles, rate, deduction), caveats to
  review before filing, and a per-vehicle table. The annual workbook adds
  odometer reconciliation and the standard versus actual expense comparison
  when you have entered the data for them.
- **Trips:** one row per trip with date, start and end time, duration, start
  and end location, distance in miles and kilometers, category, exclusion,
  vehicle, purpose, notes, gap, source and deduction, ending in a total row.
- **Expenses** (annual report only): date, vehicle, category, amount, tax
  treatment and notes, ending in a total row.

The Trips **Export** menu writes the same trip columns as CSV or XLSX.

### US deduction limits

Deduction figures use the IRS standard mileage rates saved in **Settings**.
They are estimates for US filers, not tax advice. A year with no saved rate
is priced with the most recent earlier year's rate (its later rate, if that
year changed mid-year). The dashboard, Trips, Stats, Settings, the report and
workbook, and the monthly summary and filing reminder emails name the year
and rate used until you add one. With no earlier rate either, the deduction
is unavailable.

Odograph does not decide whether a trip is deductible, calculate MACRS or
Section 179 depreciation, determine basis or recapture, or recommend a filing
method. Review the report's caveats and
[IRS Publication 463](https://www.irs.gov/publications/p463) and
[IRS Publication 946](https://www.irs.gov/publications/p946) before filing.
Tracking works anywhere, but these figures are not meant for other tax
systems.

## Taking data out

- **Trips Export, CSV or XLSX:** the trips the archive filters match.
- **Report, Export XLSX:** the report, as [above](#report-workbooks).
- **Settings > Data, Download export:** a portable JSON bundle of your
  account's ledger: vehicles, places, rules, rates, trips, expenses, odometer
  readings, timezone and default-vehicle preference. It leaves out route
  geometry, raw location points, login and tracking credentials, and
  notification settings.

XLSX cells hold text as text, so a value beginning with `=` is not
evaluated. CSV values are written as they are: a spreadsheet may evaluate a
cell beginning with `=`, `+`, `-` or `@`, so check untrusted text before
opening a CSV there.

**Import** loads a bundle into an account that is still empty: no trips,
points, places, expenses or odometer readings, and only the default vehicle,
rules and rates. Import before connecting a phone. Older supported bundle
versions are accepted. **Dry run** checks the file, including the storage
allowance, without writing. A failed import changes nothing. A busy account
answers that the import is busy; submit it again yourself, as it is never
retried.

**Settings** shows storage usage; when an account has a limit, it warns at
80%. At the limit, new data is refused or, for road matching and addresses,
paused; nothing is deleted, and viewing, exporting and removing data still
work ([storage limits](configuration.md#logical-storage-accounting)).

None of these exports is a database backup; see [Backups](backups.md).

## A point arrived, but no trip appeared

**Settings > Device status** shows whether the phone is reaching Odograph. A
recent point means ingestion works and the detector may still be waiting
for the trip to end; see
[what ends a trip](detection.md#what-happens-when-gps-reception-disappears).
Sparse or queued uploads delay it. Check **Trips** for the date and
**Needs attention** for a possible missing trip.

For trips that are late or rough, see
[battery and accuracy](detection.md#how-much-battery-does-the-tracking-consume)
and the phone settings in [Connecting OwnTracks](owntracks.md). If a total
looks wrong, check the trip's category, vehicle and exclusion, the report's
caveats, and whether a rate is saved.

Report bugs on the
[issue tracker](https://github.com/MeandrousShark/odograph/issues), without
`.env` values, passwords or real location history, and security problems as
described in [SECURITY.md](../SECURITY.md).
