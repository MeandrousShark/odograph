"""Complete streamed text MIME and stdlib-compatible SMTP envelope decisions."""
from __future__ import annotations

import base64
import os
import smtplib
from email.message import EmailMessage
from email.generator import BytesGenerator
from email import policy, utils, quoprimime

CHUNK = 65536


def checked_headers(sender, recipients, subject):
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, recipients, subject
    return msg


def file_chunks(stream):
    stream.seek(0)
    while chunk := stream.read(CHUNK):
        yield chunk


def normalize(chunks, sink):
    pending_cr = False
    last_lf = False
    written = False
    for chunk in chunks:
        output = bytearray()
        for byte in chunk:
            if pending_cr:
                output.append(10)
                written = last_lf = True
                pending_cr = False
                if byte == 10:
                    continue
            if byte == 13:
                pending_cr = True
            else:
                output.append(byte)
                written = True
                last_lf = byte == 10
        sink.write(output)
    if pending_cr:
        sink.write(b'\n')
        written = last_lf = True
    if not written or not last_lf:
        sink.write(b'\n')


def qp_chunks(chunks, linesep=b'\n'):
    """Exact quoprimime.body_encode(maxlinelen=78), at most 80 translated bytes."""
    line = ''
    for chunk in chunks:
        for value in chunk:
            if value == 10:
                if line and line[-1] in ' \t':
                    room = 79 - len(line)
                    tail = quoprimime.quote(line[-1])
                    if room == 2:
                        tail = line[-1] + '=\n'
                    elif room < 2:
                        tail = '=\n' + tail
                    line = line[:-1] + tail
                yield line.replace('\n', linesep.decode()).encode('ascii') + linesep
                line = ''
                continue
            line += quoprimime._QUOPRI_BODY_ENCODE_MAP[value]
            while len(line) >= 79:
                if line[75] == '=':
                    yield line[:76].encode('ascii') + linesep
                    line = line[75:]
                elif line[76] == '=':
                    yield line[:77].encode('ascii') + linesep
                    line = line[76:]
                else:
                    yield line[:77].encode('ascii') + b'=' + linesep
                    line = line[77:]
    if line:
        raise AssertionError('normalized input must end with LF')


def first_ten_lines(chunks):
    lines = 0
    for chunk in chunks:
        if lines + chunk.count(b'\n') < 10:
            lines += chunk.count(b'\n')
            yield chunk
            continue
        offset = 0
        while lines < 10:
            offset = chunk.index(b'\n', offset) + 1
            lines += 1
        yield chunk[:offset]
        return


def choose_cte(body):
    ascii_only, longest, current, line_count, sniff_bytes = True, 0, 0, 0, 0
    for chunk in file_chunks(body):
        ascii_only &= chunk.isascii()
        for value in chunk:
            if line_count < 10:
                sniff_bytes += 1
            if value == 10:
                longest = max(longest, current)
                current = 0
                line_count += 1
            else:
                current += 1
    if longest <= 78:
        return '7bit' if ascii_only else '8bit'
    sniff_qp = sum(len(chunk) for chunk in qp_chunks(first_ten_lines(file_chunks(body))))
    sniff_b64 = 4 * ((sniff_bytes + 2) // 3) + 1
    return 'base64' if sniff_qp > sniff_b64 else 'quoted-printable'


def base64_chunks(chunks):
    # Python's default max_line_length=78 means 57 source bytes per line.
    pending = bytearray()
    for chunk in chunks:
        pending.extend(chunk)
        whole = len(pending) // 57 * 57
        for offset in range(0, whole, 57):
            yield base64.b64encode(pending[offset:offset + 57]) + b'\r\n'
        del pending[:whole]
    if pending:
        yield base64.b64encode(pending) + b'\r\n'


def write_mime(body, header, sink, international=False):
    cte = choose_cte(body)
    for name in ('Content-Type', 'Content-Transfer-Encoding', 'MIME-Version'):
        del header[name]
    header['Content-Type'] = 'text/plain; charset="utf-8"'
    header['Content-Transfer-Encoding'] = cte
    header['MIME-Version'] = '1.0'
    del header['Bcc']
    del header['Resent-Bcc']
    header.set_payload('')
    selected_policy = header.policy.clone(utf8=True) if international else header.policy
    BytesGenerator(sink, policy=selected_policy).flatten(header, linesep='\r\n')
    if cte == 'base64':
        encoded = base64_chunks(file_chunks(body))
    elif cte == 'quoted-printable':
        encoded = qp_chunks(file_chunks(body), b'\r\n')
    else:
        encoded = (chunk.replace(b'\n', b'\r\n') for chunk in file_chunks(body))
    for chunk in encoded:
        sink.write(chunk)
    return cte


def envelope(header):
    resent = header.get_all('Resent-Date')
    if resent is None:
        prefix = ''
    elif len(resent) == 1:
        prefix = 'Resent-'
    else:
        raise ValueError("message has more than one 'Resent-' header block")
    sender = header[prefix + 'Sender'] if prefix + 'Sender' in header else header[prefix + 'From']
    sender = utils.getaddresses([sender])[0][1]
    fields = [header[prefix + field] for field in ('To', 'Bcc', 'Cc') if header[prefix + field] is not None]
    recipients = [value[1] for value in utils.getaddresses(fields)]
    try:
        ''.join([sender, *recipients]).encode('ascii')
        international = False
    except UnicodeEncodeError:
        international = True
    return sender, recipients, international


def dot_chunks(chunks):
    beginning = True
    tail = b''
    for chunk in chunks:
        output = bytearray()
        for byte in chunk:
            if beginning and byte == 46:
                output.append(46)
            output.append(byte)
            beginning = byte == 10
        tail = (tail + chunk)[-2:]
        yield bytes(output)
    if tail != b'\r\n':
        yield b'\r\n'
    yield b'.\r\n'


def send_spool(smtp, sender, recipients, mime, international):
    """Preserve smtplib sendmail acceptance/refusal/session decisions, streaming DATA."""
    smtp.ehlo_or_helo_if_needed()
    if international and not smtp.has_extn('smtputf8'):
        raise smtplib.SMTPNotSupportedError('internationalized email capability unavailable')
    options = []
    if smtp.does_esmtp:
        if smtp.has_extn('size'):
            mime.seek(0, os.SEEK_END)
            options.append('size=%d' % mime.tell())
        if international:
            options += ['SMTPUTF8', 'BODY=8BITMIME']
    code, response = smtp.mail(sender, options)
    if code != 250:
        smtp.close() if code == 421 else smtp._rset()
        raise smtplib.SMTPSenderRefused(code, response, sender)
    refused = {}
    for recipient in recipients:
        code, response = smtp.rcpt(recipient, ())
        if code not in (250, 251):
            refused[recipient] = (code, response)
        if code == 421:
            smtp.close()
            raise smtplib.SMTPRecipientsRefused(refused)
    if len(refused) == len(recipients):
        smtp._rset()
        raise smtplib.SMTPRecipientsRefused(refused)
    smtp.putcmd('data')
    code, response = smtp.getreply()
    if code != 354:
        raise smtplib.SMTPDataError(code, response)
    for chunk in dot_chunks(file_chunks(mime)):
        smtp.send(chunk)
    code, response = smtp.getreply()
    if code != 250:
        smtp.close() if code == 421 else smtp._rset()
        raise smtplib.SMTPDataError(code, response)
    return refused


def _json_file(budget, name, value):
    import json
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
    with budget.open(name, 'wb') as sink:
        for piece in encoder.iterencode(value):
            for offset in range(0, len(piece), 16380):
                sink.write(piece[offset:offset + 16380].encode('utf8'))


def prepare_text_mail(budget, body_name, from_name, to_name, config_name, subject):
    """Run only in the isolated preparation helper, under its memory authority."""
    from email.parser import BytesParser
    with budget.open(from_name, 'rb') as source:
        sender = source.read().decode('utf8')
    with budget.open(to_name, 'rb') as source:
        recipients = source.read().decode('utf8')
    header = checked_headers(sender, recipients, subject)
    # Preserve the existing message.as_bytes()/BytesParser hop for headers.
    with budget.open('mail-header', 'wb') as sink:
        BytesGenerator(sink).flatten(header)
    with budget.open('mail-header', 'rb') as source:
        header = BytesParser(policy=policy.default).parse(source)
    env_sender, env_recipients, international = envelope(header)
    _json_file(budget, 'mail-envelope', dict(sender=env_sender,
               recipients=env_recipients, international=international))
    with budget.open(body_name, 'rb') as source, budget.open('mail-normalized', 'wb') as sink:
        normalize(file_chunks(source), sink)
    for name, utf8 in [('mail-mime', False), ('mail-mime-utf8', True)]:
        with budget.open('mail-normalized', 'rb') as source, budget.open(name, 'wb') as sink:
            write_mime(source, header, sink, utf8)
    for name in ('mail-header', 'mail-normalized'):
        budget.remove(name)
    return dict(mime='mail-mime', mime_utf8='mail-mime-utf8',
                envelope='mail-envelope', config=config_name)


def prepare_quarterly_mail(budget, body_name, from_name, to_name, config_name):
    return prepare_text_mail(budget, body_name, from_name, to_name, config_name,
                             'Odograph: log an odometer reading')
