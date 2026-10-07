"""Outbound SMTP send abstraction. Named
`mailer.py`, not `email.py`, to avoid shadowing the stdlib `email` package
that `EmailMessage` itself comes from.

stdlib `smtplib` + `email.message.EmailMessage` rather than a new
dependency: native-async SMTP buys nothing at a few emails per
month, so production sessions run in a supervised helper process with a
whole transport deadline. Admission remains held through confirmed helper
exit. An explicitly injected cooperative callable remains available for
composition tests; serialization runs through an owned thread.
"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable

from app.capacity import owned_thread

from app.smtp_helper import SMTP_TIMEOUT_S, smtp_transport
from app.smtp_supervisor import send_payload, send_prepared, serialize_payload


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
        if self.transport is smtp_transport:
            payload = await owned_thread(serialize_payload, self, message)
            await send_payload(payload)
        else:
            await owned_thread(self.transport, self, message)

    async def send_prepared(self, prepared, *, before_transport=None) -> None:
        """Send complete spool artifacts without parent MIME/config parsing."""
        if self.transport is not smtp_transport:
            raise ValueError('prepared mail requires supervised SMTP transport')
        await send_prepared(prepared, before_transport=before_transport)
