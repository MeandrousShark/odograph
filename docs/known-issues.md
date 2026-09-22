# Known issues

This page lists confirmed problems in published releases that affect normal
use, with any workaround and the release that fixes them. Only the latest
release is supported; see [Support](../README.md#support). Report new problems
as GitHub issues.

## Split trip does nothing

**Affects:** v0.6.0 through v0.11.1.
**Fixed in:** the next release, v1.0.0.

On a detected trip, **Advanced trip tools** > **Split trip** lets you pick a
point on the map, but **Confirm split** sends nothing: the page stays as it
is, with no message. The button depends on script evaluation that Odograph's
own Content Security Policy blocks, so the browser never submits the request.
No data is changed.

**Workaround:** none in the application. Leave the trip as it is and split it
after upgrading to v1.0.0. Merging trips is not affected.
