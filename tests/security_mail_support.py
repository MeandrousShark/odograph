"""Test-only prepared MIME receivers; production never parses artifacts here."""
from email import policy
from email.parser import BytesParser


def read_prepared_message(prepared):
    stack, descriptors, _ = prepared.open_descriptors()
    try:
        import json
        import os
        with os.fdopen(os.dup(descriptors[1]), 'rb') as source:
            envelope = json.load(source)
        fd = descriptors[3] if envelope['international'] else descriptors[2]
        with os.fdopen(os.dup(fd), 'rb') as source:
            return BytesParser(policy=policy.default).parse(source)
    finally:
        stack.close()


async def fixture_send_prepared(mailer, prepared, *, before_transport=None):
    if before_transport is not None:
        await before_transport()
    await mailer.send(read_prepared_message(prepared))


def configure_fake_mailer(mailer, args):
    for key, value in zip(('host', 'port', 'username', 'password', 'security',
                           'tls_insecure', 'from_addr', 'to_addr'), args, strict=True):
        setattr(mailer, key, value)


class PreparedFakeReceiver:
    async def send_prepared(self, prepared, *, before_transport=None):
        if before_transport is not None:
            await before_transport()
        message = read_prepared_message(prepared)
        # Retain existing fake receiver composition assertions, without replacing
        # the real helper's construction checks or their failure-stage tests.
        fixture_value = self.compose(str(message['Subject']),
                                     message.get_content().replace('\r\n', '\n'))
        await self.send(fixture_value)
