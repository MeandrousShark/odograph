"""Defer security-mail text and MIME work until its existing mail owner reserves."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.errors import HeaderParseError

from app.prepared_mail import PreparedMail


class SecurityMailConstructionError(RuntimeError):
    """Composition failed before the legacy serialization/transport catch."""

    def __init__(self, category):
        super().__init__('security mail construction failed')
        self.category = category


@dataclass(frozen=True)
class SecurityMailSpec:
    kind: str
    link_base: str
    token: str
    purpose: str = ''


async def _text(operation, session, key, value):
    if not isinstance(value, str):
        raise TypeError('security mail text has an invalid type')
    # Preserve Python surrogate codepoints until the original use phase.
    # JSON decoding would merge adjacent high/low surrogates before construction.
    def pieces():
        for offset in range(0, len(value), 16380):
            yield value[offset:offset + 16380].encode('utf8', 'surrogatepass')
    size = 0
    for piece in pieces():
        operation.check()
        size += len(piece)
        await asyncio.sleep(0)
    await session.send_command({'type': 'text', 'key': key, 'size': size,
                                'encoding': 'utf8-surrogatepass'})
    for piece in pieces():
        operation.check()
        await session.send_text(piece)


async def prepare_security_mail(operation, mailer, specification):
    session = await operation.start_helper('security')
    fields = dict(link_base=specification.link_base, token=specification.token,
                  purpose=specification.purpose, sender=mailer.from_addr, recipient=mailer.to_addr,
                  host=mailer.host, username=mailer.username, password=mailer.password,
                  security=mailer.security)
    for key, value in fields.items():
        try:
            await _text(operation, session, key, value)
        except TypeError as exc:
            if key in ('sender', 'recipient', 'link_base', 'token', 'purpose'):
                raise SecurityMailConstructionError(type(exc).__name__) from None
            raise
    try:
        await session.request({'type': 'construct', 'kind': specification.kind})
    except (ValueError, TypeError, OverflowError, UnicodeError, HeaderParseError) as exc:
        raise SecurityMailConstructionError(type(exc).__name__) from None
    result = await session.request({'type': 'serialize', 'port': mailer.port,
                                    'tls_insecure': mailer.tls_insecure})
    await session.finish_input()
    return PreparedMail(operation.reservation, result['artifacts'])
