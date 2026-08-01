"""Outbound SMTP send abstraction. Named
`mailer.py`, not `email.py`, to avoid shadowing the stdlib `email` package
that `EmailMessage` itself comes from.

stdlib `smtplib` + `email.message.EmailMessage` rather than a new
dependency: native-async SMTP buys nothing at a few emails per
month, so the blocking session runs via `asyncio.to_thread` instead, keeping
the event loop free for everything else the app is doing. The blocking
transport is an injectable callable (`Mailer.transport`) so unit tests can
capture a composed message or force a failure with no real network I/O and
no sleeps; the default (`smtp_transport` below) is the real thing.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable

SMTP_TIMEOUT_S = 15.0


def smtp_transport(mailer: "Mailer", message: EmailMessage) -> None:
    """Default blocking transport: connects, authenticates if credentials
    are set, and sends `message`. `SMTP_SECURITY` selects which of the three
    stdlib connection shapes to use -- `starttls` (submission port 587,
    the common case), `ssl` (implicit-TLS port 465), or `none` (a LAN relay
    with no encryption at all, e.g. a local Postfix relay-only host).

    The TLS context is *always* explicit. `smtplib.SMTP.starttls()` and
    `smtplib.SMTP_SSL` both default to `ssl._create_stdlib_context()` when
    no `context=` is given, which -- unlike `requests`/`httpx` -- does not
    verify the server's certificate. Passing `ssl.create_default_context()`
    is the one line standing between this relay connection and a trivial
    on-path credential capture. `SMTP_TLS_INSECURE=1` swaps in an unverified
    context for the one legitimate exception: a localhost bridge (Proton
    Mail Bridge) presenting a self-signed cert with no real CA to validate
    against.
    """
    context = (
        ssl._create_unverified_context() if mailer.tls_insecure
        else ssl.create_default_context()
    )
    if mailer.security == "ssl":
        with smtplib.SMTP_SSL(mailer.host, mailer.port, context=context, timeout=SMTP_TIMEOUT_S) as smtp:
            _authenticate(smtp, mailer)
            smtp.send_message(message)
    elif mailer.security == "starttls":
        with smtplib.SMTP(mailer.host, mailer.port, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.starttls(context=context)
            _authenticate(smtp, mailer)
            smtp.send_message(message)
    elif mailer.security == "none":
        with smtplib.SMTP(mailer.host, mailer.port, timeout=SMTP_TIMEOUT_S) as smtp:
            _authenticate(smtp, mailer)
            smtp.send_message(message)
    else:
        raise ValueError(f"unknown SMTP_SECURITY {mailer.security!r}")


def _authenticate(smtp: smtplib.SMTP, mailer: "Mailer") -> None:
    if mailer.username:
        smtp.login(mailer.username, mailer.password)


BlockingTransport = Callable[["Mailer", EmailMessage], None]


@dataclass
class Mailer:
    """Holds the SMTP config plus `compose`/`send`. A dataclass, not a bag of
    module-level functions, so `app/main.py`'s lifespan can build one from
    `Config` once and hand it to `EmailDigestWorker` the same way it hands
    workers an `httpx.AsyncClient`.

    Never log `smtp_password` or any string containing it -- same discipline
    that keeps `GEOCODE_API_KEY` out of logs (`app/main.py`'s httpx
    log-level note). Nothing in this module logs at all, deliberately: a
    send failure is the caller's (worker's) to log, and it must log the
    exception, never the credentials that produced it.
    """
    host: str
    port: int
    username: str
    password: str
    security: str  # "starttls" | "ssl" | "none"
    tls_insecure: bool
    from_addr: str
    to_addr: str
    transport: BlockingTransport = field(default=smtp_transport, repr=False)

    def compose(self, subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.from_addr
        message["To"] = self.to_addr
        message["Subject"] = subject
        message.set_content(body)
        return message

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self.transport, self, message)
