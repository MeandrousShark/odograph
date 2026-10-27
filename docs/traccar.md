# Connecting Traccar Client

[Traccar Client](https://www.traccar.org/client/) is a free phone app for iOS
and Android that can also feed Odograph. It posts each location straight to
your own instance, with no third-party server in between. If you can choose,
use [OwnTracks](owntracks.md): it generally gives better trip boundaries (see
below). For the application's normal first steps after installation, see the
[Odograph usage guide](usage.md).

## App configuration

Traccar Client cannot send an HTTP username and password, so the credential
travels in the URL instead.

1. Sign in to Odograph, open **Settings > Tracking**, and create a device for
   this phone. Create a separate device for each Traccar phone.
2. When the credential is issued, copy the **Traccar Server URL** shown beside
   the OwnTracks values. It looks like
   `https://your-domain/ingest/traccar?username=...&key=...`.
3. In Traccar Client, paste it into the **Server URL** setting. Alternatively,
   open the **Traccar config link** (it starts with `org.traccar.client://`) on
   the phone and Traccar Client fills in the setting for you.

Both values are shown only once, with the password. If you lose them, choose
**Replace password** and update the phone.

The **Device identifier** in Traccar Client is only a label. It does not select
the account or the device; the credential in the URL does that.

Do not use a plain `https://` QR code or link to share the setting. Traccar
Client keeps only the address and path from one, which drops the credential.
Typing the Server URL or opening the `org.traccar.client://` link keeps it.

## Heartbeat and trip boundaries

Enable a heartbeat interval in Traccar Client shorter than five minutes.
Traccar has no arrival trigger like OwnTracks region or visit fixes, and it
pauses reporting while the phone is parked. A trip ends on the five-minute
stay rule, so without a heartbeat it may not close until your next drive
starts. Even with one, trips can close late: when the phone cannot get a
fresh fix, a heartbeat repeats the last known location with its original
time, which adds nothing new.

OwnTracks generally gives better trip boundaries because of those arrival
fixes.

## What is stored

Each Traccar post is stored with every submitted field as received, such as
`id`, `lat`, `lon`, `timestamp`, `accuracy`, `altitude`, `speed`, `bearing`,
`batt`, `charge` and `alarm`, plus a marker naming Traccar as the source. The
credential in the URL is never stored, and a heartbeat without coordinates is
not stored. See the [privacy notes](privacy.md).

## Security

The credential is part of the URL, so anyone who can see the app's settings
can read it. Odograph strips the query string from its own access log. A
reverse proxy that logs full request URIs will record the key unless you drop
it. Caddy writes access logs only for a site with a `log` directive; if
yours has one, add a `format filter` with a `query` filter on `request>uri`
that deletes `key`:

```caddyfile
mileage.example.com {
    log {
        format filter {
            wrap json
            fields {
                request>uri query {
                    delete key
                }
            }
        }
    }
    reverse_proxy 127.0.0.1:8077
}
```

See [Reverse proxy](reverse-proxy.md) for the base configuration. Other proxies
need an equivalent rule to remove the `key` parameter from logged URIs.

To rotate the credential, choose **Replace password** in **Settings >
Tracking**, then paste the new Server URL into the phone. The old key stops
working immediately. **Revoke access** stops future uploads.
