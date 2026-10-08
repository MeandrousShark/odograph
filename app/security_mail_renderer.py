"""Security-mail composition and serialization inside the preparation helper."""
from __future__ import annotations

from email.message import EmailMessage
import json
from urllib.parse import quote

from app.mime_preparation import prepare_text_mail

_KEYS = {'link_base', 'token', 'purpose', 'sender', 'recipient', 'host',
         'username', 'password', 'security'}


class Renderer:
    def __init__(self, budget):
        self.budget = budget
        self.present = set()
        self.constructed = False

    def text(self, command, channel):
        key, size = command['key'], command['size']
        if (key not in _KEYS or key in self.present or type(size) is not int or size < 0
                or command.get('encoding') != 'utf8-surrogatepass'):
            raise ValueError('invalid security mail text')
        with self.budget.open('security-' + key, 'wb') as sink:
            left = size
            while left:
                chunk = channel.recv_text()
                if not chunk or len(chunk) > left:
                    raise ValueError('invalid security mail frame')
                sink.write(chunk)
                left -= len(chunk)
        self.present.add(key)

    def whole(self, key):
        with self.budget.open('security-' + key, 'rb') as source:
            return source.read().decode('utf8', 'surrogatepass')

    def construct(self, kind):
        base, token, purpose = self.whole('link_base'), self.whole('token'), self.whole('purpose')
        if kind == 'reset':
            self.subject = 'Reset your Odograph password'
            link = f'{base}/reset-password#token={token}'
            body = ('Someone asked to reset the password for your Odograph account.\n\n'
                    f'To choose a new password, open this link:\n{link}\n\n'
                    f'If the link does not fill the form, enter this code manually: {token}\n\n'
                    'The code expires in 30 minutes and works once. Resetting your password signs '
                    'you out of Odograph everywhere. It does not change your sign-in provider or '
                    'tracking devices. If you did not ask for this, ignore this email.')
        elif kind == 'invitation':
            self.subject = 'Invitation to Odograph'
            link = f'{base}/invite#token={quote(token, safe="")}'
            body = ('An administrator invited you to join Odograph.\n\n'
                    f'Open this link to accept the invitation:\n{link}\n\n'
                    f'If the link does not fill the form, enter this one-time token manually:\n{token}\n\n'
                    'The invitation expires in 48 hours and can be used once.')
        elif kind == 'challenge':
            self.subject = 'Confirm your Odograph email address'
            link = f'{base}/settings/account/email/confirm#purpose={purpose}&token={token}'
            body = ('To confirm your email address, open this link while signed in:\n'
                    f'{link}\n\nIf the link does not fill the form, choose {purpose} '
                    f'and enter this code manually: {token}\n\n'
                    'The code expires in 30 minutes. If you did not request this, ignore this email.')
        else:
            raise ValueError('unknown security mail kind')
        message = EmailMessage()
        message['From'] = self.whole('sender')
        message['To'] = self.whole('recipient')
        message['Subject'] = self.subject
        message.set_content(body)
        with self.budget.open('mail-body', 'wb') as sink:
            for offset in range(0, len(body), 16380):
                sink.write(body[offset:offset + 16380].encode('utf8'))
        message.set_payload('')
        self.header = message
        self.constructed = True
        return {}

    def serialize(self, command):
        if not self.constructed:
            raise ValueError('security mail is not constructed')
        sender, recipient = self.whole('sender'), self.whole('recipient')
        try:
            sender.encode('utf8')
            recipient.encode('utf8')
        except UnicodeEncodeError:
            surrogate_headers = True
        else:
            surrogate_headers = False
            for value, name in ((sender, 'mail-from'), (recipient, 'mail-to')):
                with self.budget.open(name, 'wb') as sink:
                    for offset in range(0, len(value), 16380):
                        sink.write(value[offset:offset + 16380].encode('utf8'))
        config = {key: self.whole(key) for key in ('host', 'username', 'password', 'security')}
        config.update(port=command['port'], tls_insecure=command['tls_insecure'])
        with self.budget.open('mail-config', 'wb') as sink:
            for piece in json.JSONEncoder(ensure_ascii=True, separators=(',', ':')).iterencode(config):
                for offset in range(0, len(piece), 4096):
                    sink.write(piece[offset:offset + 4096].encode('ascii'))
        if surrogate_headers:
            from app.notification_renderer import prepare_checked_mail
            return {'artifacts': prepare_checked_mail(self.budget, self.header)}
        return {'artifacts': prepare_text_mail(self.budget, 'mail-body', 'mail-from', 'mail-to',
                                                'mail-config', self.subject)}


def render(channel, budget):
    renderer = Renderer(budget)
    while (command := channel.recv_command()) is not None:
        if command['type'] == 'text':
            renderer.text(command, channel)
        elif command['type'] == 'construct':
            channel.send_response(renderer.construct(command['kind']))
        elif command['type'] == 'serialize':
            channel.send_response(renderer.serialize(command))
            return
        else:
            raise ValueError('unknown security mail command')
