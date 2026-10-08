"""Complete independent stdlib message and SMTP wire oracles."""
import pytest
pytestmark = pytest.mark.unit
import io
import smtplib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from app.mime_preparation import checked_headers, normalize, write_mime, envelope, send_spool

class SMTP(smtplib.SMTP):
    def __init__(self, international=True, reject=()):
        self.does_esmtp = True
        self.esmtp_features = {'size': '999999999'}
        if international:
            self.esmtp_features['smtputf8'] = ''
        self.reject = set(reject)
        self.commands, self.wire = [], bytearray()
        self.responses = iter([(354, b'ready'), (250, b'accepted')])
    def ehlo_or_helo_if_needed(self): pass
    def mail(self, sender, options=()):
        self.commands.append(('mail', sender, list(options)))
        return 250, b'ok'
    def rcpt(self, recipient, options=()):
        self.commands.append(('rcpt', recipient, list(options)))
        return (550, b'refused') if recipient in self.reject else (250, b'ok')
    def putcmd(self, command): self.commands.append((command,))
    def getreply(self): return next(self.responses)
    def send(self, chunk): self.wire.extend(chunk)
    def close(self): self.commands.append(('close',))
    def _rset(self): self.commands.append(('rset',))
    def data(self, message):
        self.putcmd('data')
        code, response = self.getreply()
        assert code == 354
        wire = smtplib._quote_periods(message)
        self.send(wire + (b'' if wire.endswith(b'\r\n') else b'\r\n') + b'.\r\n')
        return self.getreply()


def test_complete_mime_and_envelope_parity():
    bodies = ['', 'ascii', '.dot\n..two\rsolo\r\ncombined\n', 'é好',
              ' ' * 77 + '\n', ' ' * 78 + '\n', ' ' * 79 + '\n',
              'a' * 300, 'é' * 300, ('abc = é \t\n' * 12),
              'A' * 74 + '=\t\n', 'A' * 75 + 'é\n', 'A' * 76 + 'é\n',
              '\v\f\x85\u2028\u2029', '. ' * 5000]
    headers = [ ('sender@example.test', 'one@example.test', 'plain'),
                ('Séndér <sender@example.test>', '"Surname, Given" <one@example.test>, Group: two@example.test, three@example.test;', 'unicode subject é'),
                ('发件人 <用户@example.test>', '收件人 <收件人@example.test>', 'UTF8 subject 好'),
                ('sender@example.test', 'not-an-address, duplicate@example.test, duplicate@example.test', 'legacy parser') ]
    count, encodings = 0, set()
    for body in bodies:
        for sender, recipients, subject in headers:
            for split in [1, 2, 3, 77, 256, 65536]:
                original = EmailMessage()
                original['From'], original['To'], original['Subject'] = sender, recipients, subject
                original.set_content(body)
                # Preserve the existing MIME serialize/parse hop as the oracle.
                baseline = BytesParser(policy=policy.default).parsebytes(original.as_bytes())
                old_smtp = SMTP()
                expected_result = old_smtp.send_message(baseline)
                header = checked_headers(sender, recipients, subject)
                # The existing header serializer/parser hop is bounded here.
                header = BytesParser(policy=policy.default).parsebytes(header.as_bytes())
                normalized, mime = io.BytesIO(), io.BytesIO()
                encoded = body.encode('utf8')
                normalize((encoded[i:i + split] for i in range(0, len(encoded), split)), normalized)
                env_sender, env_recipients, international = envelope(header)
                cte = write_mime(normalized, header, mime, international)
                new_smtp = SMTP()
                actual_result = send_spool(new_smtp, env_sender, env_recipients, mime, international)
                assert expected_result == actual_result
                assert old_smtp.commands == new_smtp.commands, (old_smtp.commands, new_smtp.commands)
                assert old_smtp.wire == new_smtp.wire, (body[:20], cte, old_smtp.wire[:100], new_smtp.wire[:100])
                count += 1
                encodings.add(cte)
    # Missing SMTPUTF8 retains the baseline failure before MAIL/DATA.
    for support in [False, True]:
        original = EmailMessage()
        original['From'], original['To'], original['Subject'] = '用户@example.test', 'one@example.test', 'subject'
        original.set_content('body')
        try:
            SMTP(international=support).send_message(original)
            expected = None
        except Exception as exc:
            expected = type(exc).__name__
        header = checked_headers(str(original['From']), str(original['To']), str(original['Subject']))
        normalized, mime = io.BytesIO(b'body\n'), io.BytesIO()
        sender, recipients, international = envelope(header)
        write_mime(normalized, header, mime, international)
        try:
            send_spool(SMTP(international=support), sender, recipients, mime, international)
            actual = None
        except Exception as exc:
            actual = type(exc).__name__
        assert expected == actual
    assert count == 360
    assert encodings == {'7bit', '8bit', 'quoted-printable', 'base64'}

class OracleSMTP(SMTP):
    def __init__(self, *, support=True, size=True, mail=250, rcpt=None, data=(354,250)):
        super().__init__(support)
        if not size: self.esmtp_features.pop('size')
        self.mail_code, self.rcpt_codes = mail, rcpt or {}
        self.responses = iter([(data[0],b'ready'),(data[1],b'accepted')])
    def mail(self, sender, options=()):
        self.commands.append(('mail',sender,list(options)))
        return self.mail_code,b'ok'
    def rcpt(self, recipient, options=()):
        self.commands.append(('rcpt',recipient,list(options)))
        return self.rcpt_codes.get(recipient,250),b'reply'
    def data(self, message):
        self.putcmd('data'); code,response=self.getreply()
        if code!=354: raise smtplib.SMTPDataError(code,response)
        self.send(smtplib._quote_periods(message)+(b'' if message.endswith(b'\r\n') else b'\r\n')+b'.\r\n')
        return self.getreply()

def outcome(f):
    try: return ('result',f())
    except Exception as exc: return ('error',type(exc).__name__,exc.args)

def test_sender_resent_refusal_size_and_duplicate_parity():
    headers=[[], [('Cc','Group: second@example.test, third@example.test;'),('Bcc','hidden@example.test')],
        [('Sender','actual@example.test')], [('Resent-Date','Wed, 07 Oct 2026 12:00:00 +0000'),('Resent-From','resent@example.test'),('Resent-To','one@example.test'),('Resent-Cc','second@example.test'),('Resent-Bcc','hidden@example.test'),('Resent-Sender','sender2@example.test')],
        [('Resent-Date','Wed, 07 Oct 2026 12:00:00 +0000'),('Resent-Date','Tue, 06 Oct 2026 12:00:00 +0000')],
        [('Cc','组: 用户@example.test, second@example.test;'),('Bcc','hidden@example.test')],
        [('To','one@example.test, one@example.test'),('Bcc','hidden@example.test')]]
    scenarios=[{}, {'size':False},{'support':False},{'rcpt':{'one@example.test':550}}, {'rcpt':{'one@example.test':550,'second@example.test':550,'third@example.test':550,'hidden@example.test':550,'用户@example.test':550}}, {'rcpt':{'one@example.test':251}}, {'rcpt':{'one@example.test':421}}, {'mail':550},{'mail':421},{'data':(550,250)},{'data':(354,550)},{'data':(354,421)}]
    cases=0
    for fields in headers:
        for options in scenarios:
            msg=EmailMessage(); msg['From']='from@example.test';msg['To']='one@example.test';msg['Subject']='hello é'
            for key,value in fields:
                if key=='To': del msg['To']
                msg[key]=value
            msg.set_content('.one\n..two\né\n')
            baseline=BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
            old=OracleSMTP(**options); expected=outcome(lambda:old.send_message(baseline))
            header=EmailMessage()
            for key,value in msg.items():
                if key not in ('Content-Type','Content-Transfer-Encoding','MIME-Version'):header[key]=value
            header=BytesParser(policy=policy.default).parsebytes(header.as_bytes())
            new=OracleSMTP(**options)
            def run():
                sender,recipients,international=envelope(header)
                mime=io.BytesIO();write_mime(io.BytesIO('.one\n..two\né\n'.encode()),header,mime,international)
                return send_spool(new,sender,recipients,mime,international)
            actual=outcome(run)
            # Error prose differs intentionally; type, code, address/refusal payload must match.
            assert expected[:2]==actual[:2],(fields,options,expected,actual)
            if expected[0]=='result':assert expected==actual
            elif expected[1] not in ('SMTPNotSupportedError','ValueError'):assert expected==actual,(expected,actual)
            assert old.commands==new.commands,(fields,options,old.commands,new.commands)
            assert old.wire==new.wire
            cases+=1
    assert cases == 84
