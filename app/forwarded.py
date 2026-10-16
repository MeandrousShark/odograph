"""Client address from X-Forwarded-For under FORWARDED_ALLOW_IPS=*.

Uvicorn's --proxy-headers handling walks X-Forwarded-For right to left past
trusted proxies when FORWARDED_ALLOW_IPS lists exact addresses or CIDRs. Under
the bare wildcard it trusts every hop and returns the leftmost entry instead.
A proxy that appends to an incoming X-Forwarded-For leaves that leftmost entry
under the control of the request sender, while the rightmost entry is the
address the immediate proxy saw.

RightmostForwardedForMiddleware runs inside uvicorn's middleware, so it sees
scope["client"] already set to the leftmost entry and the original header
still in scope["headers"]. Every hop is trusted under the wildcard, so it
recomputes the client from the rightmost entry. Scheme handling stays with
uvicorn, and the Forwarded header is ignored, as uvicorn ignores it.

The trust setting comes from the FORWARDED_ALLOW_IPS environment variable,
which uvicorn also reads; the image passes --proxy-headers and no
--forwarded-allow-ips option. uvicorn enables proxy headers by default, so a
plain `uvicorn app.main:create_app --factory` behaves the same way. The app
cannot see whether uvicorn ran with --no-proxy-headers, and applies the rule
under `*` regardless: `*` already declares every peer trusted.
"""
from __future__ import annotations


def wildcard_trust(forwarded_allow_ips: str) -> bool:
    """True only for the value uvicorn itself treats as trust-everything.

    Uvicorn compares the raw setting with "*". A padded value such as " * "
    or a list such as "127.0.0.1,*" is parsed as a host list in which "*" is a
    literal that matches no peer address, so neither is a wildcard here.
    """
    return forwarded_allow_ips == "*"


def _parse_host_port(value: str) -> tuple[str, int]:
    # Same forms uvicorn accepts: bare host, IPv4 host:port, [IPv6] and
    # [IPv6]:port. Anything else is returned whole with port 0.
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, 0
        host, remainder = value[1:end], value[end + 1:]
        if not remainder:
            return host, 0
        if not remainder.startswith(":"):
            return value, 0
        try:
            return host, int(remainder[1:])
        except ValueError:
            return host, 0
    if value.count(":") == 1:
        host, port = value.split(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return value, 0
    return value, 0


def rightmost_forwarded_for(values: list[bytes]) -> tuple[str, int] | None:
    """Client address from the last non-empty X-Forwarded-For entry.

    Multiple header lines are combined in order, as uvicorn does. Like
    uvicorn's own choice of entry, the value is not validated as an IP
    address. Returns None when every entry is empty.
    """
    combined = b", ".join(values).decode("latin1")
    for entry in reversed(combined.split(",")):
        entry = entry.strip()
        if entry:
            host, port = _parse_host_port(entry)
            return host or entry, port
    return None


class RightmostForwardedForMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            values = [value for name, value in scope["headers"] if name == b"x-forwarded-for"]
            if values:
                client = rightmost_forwarded_for(values)
                if client is not None:
                    # In place, so uvicorn's access log sees the same address.
                    scope["client"] = client
        await self.app(scope, receive, send)
