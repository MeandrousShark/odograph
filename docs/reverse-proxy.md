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
public `https://` URL before visiting `/signup` or `/login`; plain
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
    listen 443 ssl http2;
    server_name mileage.example.com;

    ssl_certificate     /etc/letsencrypt/live/mileage.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mileage.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8077;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

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

Multiple trusted entries may be comma-separated. Prefer exact peer addresses
or the narrowest practical private CIDR whenever the loopback-only safety
boundary no longer applies.
