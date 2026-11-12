# Known issues

Confirmed problems in published releases that affect normal use, with any
workaround and the release that fixes them. See the
[support policy](upgrading.md#support-policy) for which releases are
supported.

## Places heatmap could not load

**Affects:** v1.5.0.
**Fixed in:** v1.5.1.

For an account with an avatar picture, **Places** often showed "The heatmap
could not load. Change a control to try again." when the page opened. The
page asks for the header's avatar and the heatmap at the same moment, and
the server turned the second request away as busy. Sometimes the avatar
lost instead and did not show, and changing a control quickly could also
end in the message. No data is affected.

**Workaround:** change any heatmap control, such as the mode or a date; the
heatmap then loads.

## Split trip does nothing

**Affects:** v0.6.0 through v0.11.1.
**Fixed in:** v0.11.2.

On a detected trip, **Advanced trip tools** > **Split** lets you pick a
point on the map, but **Confirm split** sends nothing: the page stays as it
is, with no message. The button depends on script evaluation that Odograph's
own Content Security Policy blocks, so the browser never submits the request.
No data is changed.

**Workaround:** none in the application. Upgrade to v0.11.2 or later, then
split the trip. Merging trips is not affected.
