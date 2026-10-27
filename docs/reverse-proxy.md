# Reverse proxy and TLS

Use this guide to give Odograph a secure HTTPS address. A reverse proxy accepts
browser and phone connections, handles the HTTPS certificate, and passes
requests to Odograph. In the standard setup, Odograph's own port is reachable
only from the same host.

See [security.md](security.md) for the full hardening checklist this guide
feeds into, including HSTS and the `FORWARDED_ALLOW_IPS` risk this document
covers in depth below.

The canonical compose file publishes the application only on
`127.0.0.1:8077`. Run the reverse proxy on the same host, terminate TLS there,
and proxy to that loopback address. Port 8077 is not intended to be exposed to
the internet directly.

Production browser sessions use Secure cookies. Complete TLS setup and use the
public `https://` URL before visiting `/signup`, `/login` or `/invite`; plain
`http://127.0.0.1:8077` is suitable only for health checks and as the proxy
upstream. Use a certificate trusted by both the browser and the OwnTracks
phone. Caddy can obtain one automatically for a public domain; nginx commonly
uses a certificate provisioned by a tool such as Certbot.

## Caddy

Replace the example hostname after its DNS records point to the host:

```caddyfile
mileage.example.com {
    reverse_proxy 127.0.0.1:8077
}
```

Caddy supplies the forwarded host, client, and scheme headers expected by the
application and manages public TLS automatically under its normal HTTPS
configuration.

If phones use Traccar Client, its credential is in the request query string.
See [Connecting Traccar Client](traccar.md#security) to keep it out of the
proxy log.

## nginx

This example assumes the certificate and key already exist. Replace the
hostname and certificate paths for your installation:

```nginx
server {
    listen 80;
    server_name mileage.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name mileage.example.com;
    client_max_body_size 51m;

    ssl_certificate     /etc/letsencrypt/live/mileage.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mileage.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8077;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`http2 on;` needs nginx 1.25.1 or newer; on older versions use
`listen 443 ssl http2;` instead. Portable imports accept up to
`PORTABLE_IMPORT_MAX_BYTES` (50 MiB by default) plus a small multipart
allowance (`CAPACITY_MULTIPART_OVERHEAD_BYTES`, 64 KiB), while nginx limits
the whole request body to 1 MiB unless `client_max_body_size` is raised. Set it
at least 1 MiB above the import limit, as above. See the
[configuration reference](configuration.md#ui-reports-and-imports).

## Trusting forwarded headers

`FORWARDED_ALLOW_IPS` controls which immediate network peers uvicorn trusts to
supply `X-Forwarded-For` and `X-Forwarded-Proto`. Trust matters because these
headers determine the recorded client address and whether generated URLs use
the external HTTPS scheme.

The shipped `.env.example` uses:

```dotenv
FORWARDED_ALLOW_IPS=*
```

That wildcard is safe only in the canonical topology: compose publishes port
8077 on host loopback, so an external client cannot connect to the application
without passing through a local service controlled by the operator. A host
proxy targets `127.0.0.1:8077`, but container port translation may make that
connection appear to uvicorn as a runtime-specific bridge or gateway address
rather than `127.0.0.1`; the wildcard handles both Docker and Podman without
guessing a private subnet.

Under `FORWARDED_ALLOW_IPS=*`, the client address is the rightmost
`X-Forwarded-For` entry: the address the immediate proxy saw. Entries a client
sent ahead of it are ignored, whether the proxy replaces the header or appends
to it. Multiple `X-Forwarded-For` header lines are read in order as one list.
If every entry is empty, the connecting address is used. The rightmost entry
is used as written, without checking that it is an IP address. The wildcard
must be exactly `*`; a value such as `127.0.0.1,*` is a list of exact
addresses, as it is for uvicorn. With exact addresses or CIDRs, uvicorn walks
the list from the right and uses the first address it does not trust, which
is the address the outermost trusted proxy saw. `X-Forwarded-Proto` handling
is the same in both cases, and the `Forwarded` header is not used.
The application reads the same `FORWARDED_ALLOW_IPS` environment variable as
uvicorn, so set trust there rather than with uvicorn's `--forwarded-allow-ips`
option.

Having the proxy replace `X-Forwarded-For` with the address that connected to
it, rather than append to the incoming header, is still recommended. The
application then receives one value set by the proxy, and other tools that
read the header, such as logs or a later proxy, see the same address. The
Caddy example above already replaces the header; the nginx example does so
with `$remote_addr`.

Change the setting when the topology changes:

- If port 8077 is bound to a non-loopback address, firewall it so only the
  proxy can connect and set `FORWARDED_ALLOW_IPS` to the immediate proxy IP or
  CIDR as the application container actually sees it. Do not leave `*` on an
  internet-reachable application port.
- If the proxy runs in a container, `127.0.0.1` means the proxy container
  itself, not the host or application. Connect it through a deliberately
  shared, private container network (or the runtime's host-gateway address),
  then trust only that proxy's stable IP or private proxy-network CIDR. Keep
  untrusted workloads off that network.
- If the proxy is on another host, change the compose publish address to a
  private interface, restrict the host firewall to the remote proxy, and trust
  only the immediate source IP/CIDR seen after any host or container NAT.
- If another proxy, CDN or tunnel sits in front of your reverse proxy, the
  rightmost entry under `*` is the outer proxy's address, so every client
  shares one recorded address and one sign-in rate limit. Set
  `FORWARDED_ALLOW_IPS` to the exact addresses or CIDRs of every proxy in the
  chain as the application sees them, so uvicorn skips them from the right.

Multiple trusted entries may be comma-separated. Prefer exact peer addresses
or the narrowest practical private CIDR whenever the loopback-only safety
boundary no longer applies.
