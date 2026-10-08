import json
import time
import asyncio
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy

import pytest

from app.email_digest import _render
from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.notification_preparation import Projection
from app.notification_renderer import Renderer, render
from app.preparation import PreparationOperation
from app.preparation_resources import ResourceBudget, SpoolReservation

pytestmark = pytest.mark.unit


@pytest.fixture
def renderer(tmp_path):
    reservation = SpoolReservation.acquire(tmp_path / 'spool', time.monotonic() + 2)
    budget = ResourceBudget(reservation.directory, reservation.directory_fd)
    value = Renderer(budget)
    try:
        yield value
    finally:
        value.close()
        budget.close()
        reservation.release()


def text(renderer, key, value, frame=65520):
    raw = value.encode('utf8')
    class Channel:
        def __init__(self): self.position = 0
        def recv_text(self):
            result = raw[self.position:self.position + frame]
            self.position += len(result)
            return result
    renderer.text(key, len(raw), Channel())


@pytest.mark.parametrize('count', [1, 257, 10000, 50000])
@pytest.mark.parametrize('url', ['', 'https://example.invalid/trailing/'])
def test_complete_quarterly_body_matches_template_and_python_sort(renderer, count, url):
    names = [('車ßZéa😀\n.dot', 'a', 'A', 'é', 'e\u0301', '', 'duplicate')[i % 7] + str(i % 23)
             for i in range(count)]
    names[0] = '😀é\r\n.line<&' * 12000
    text(renderer, 'app_url', url, frame=3)
    renderer.initialized = True
    for identity, name in enumerate(reversed(names)):
        text(renderer, 'vehicle_name', name, frame=65519)
        renderer.vehicle(identity)
    renderer.body()
    expected = _render('quarterly_odometer.txt', vehicles=', '.join(sorted(names)),
        noun='vehicle' if count == 1 else 'vehicles', settings_url=url + '/settings' if url else '')
    with renderer.budget.open('mail-body', 'rb') as source:
        assert source.read() == expected.encode('utf8')
    assert renderer.budget.verify('mail-body')[1] == len(expected.encode('utf8'))


def test_preferences_compare_complete_utf8_bytes_without_normalization(renderer):
    value = 'é😀<&,"' * 50000
    for key in ('email_to', 'current_email_to'):
        text(renderer, key, value, frame=77)
    assert renderer.equal('email_to', 'current_email_to')
    text(renderer, 'current_email_to', value[:-1] + '!')
    assert not renderer.equal('email_to', 'current_email_to')
    text(renderer, 'display_tz', 'UTC')
    text(renderer, 'current_display_tz', 'Etc/UTC')
    assert not renderer.equal('display_tz', 'current_display_tz')


@pytest.mark.parametrize('codec,value',[('shift_jis','¥〜−'),('latin-1','éÿ'),('utf8','😀é')])
def test_client_decoding_is_complete_before_utf8_storage(renderer,codec,value):
    raw = value.encode(codec)
    class Channel:
        def __init__(self): self.offset = 0
        def recv_text(self):
            chunk = raw[self.offset:self.offset+1]
            self.offset += 1
            return chunk
    renderer.text('email_to',len(raw),Channel(),codec)
    assert renderer.whole('email_to') == raw.decode(codec)
    text(renderer,'current_email_to',raw.decode(codec))
    assert renderer.equal('email_to','current_email_to')


@pytest.mark.parametrize('keep',[True,False])
def test_invalid_suffix_never_exposes_partial_decoded_reference(renderer,keep):
    class Channel:
        def __init__(self): self.chunks = [b'valid'*10000,b'\xf0\x9f']
        def recv_text(self): return self.chunks.pop(0)
    with pytest.raises(UnicodeDecodeError):
        renderer.text('email_to',50002,Channel(),'utf8',keep)
    assert 'email_to' not in renderer.refs


def test_logged_name_is_validated_without_retaining_or_sorting_it(renderer):
    raw = ('😀車'*100000).encode('utf8')
    class Channel:
        def __init__(self): self.offset = 0
        def recv_text(self):
            chunk = raw[self.offset:self.offset+65520]
            self.offset += len(chunk)
            return chunk
    renderer.initialized = True
    before = renderer.texts.tell()
    renderer.text('vehicle_name',len(raw),Channel(),'utf8',False)
    renderer.vehicle(1,False)
    assert renderer.texts.tell() == before and not renderer.vehicles


def test_zero_due_avoids_body_config_and_header_construction(renderer):
    renderer.initialized = True
    text(renderer, 'email_from', 'invalid\nheader')
    assert renderer.prepare() == {'due_count': 0, 'artifacts': None}
    assert not (renderer.budget.directory / 'mail-body').exists()
    assert not (renderer.budget.directory / 'mail-config').exists()


def test_timezone_boundary_preserves_local_dst_and_hour(renderer):
    text(renderer, 'timezone_paths', json.dumps(__import__('zoneinfo').TZPATH))
    text(renderer, 'display_tz', 'America/Los_Angeles')
    result = renderer.initialize({'now': '2026-10-01T15:59:59+00:00', 'hour': 9,
        'port': 587, 'tls_insecure': False})
    assert result == {}
    assert renderer.quarter_bounds(9) == {'quarter_start': '2026-07-01T09:00:00-07:00'}


def test_generic_capture_does_not_evaluate_a_kind_boundary(renderer):
    text(renderer,'timezone_paths',json.dumps(__import__('zoneinfo').TZPATH))
    text(renderer,'display_tz','UTC')
    command = {'now':'0001-01-01T08:00:00+00:00','hour':9,'port':25,'tls_insecure':False}
    assert renderer.initialize(command) == {}
    assert renderer.now.isoformat() == command['now']
    with pytest.raises(ValueError):
        renderer.quarter_bounds(9)


@pytest.mark.parametrize('size,chunks', [(4, [b'a']), (1, [b'aa']), (-1, [])])
def test_invalid_text_length_fails_without_success(renderer, size, chunks):
    class Channel:
        def recv_text(self): return chunks.pop(0) if chunks else b''
    with pytest.raises(ValueError):
        renderer.text('email_to', size, Channel())


def test_dispatch_closes_renderer_on_unknown_command(tmp_path):
    reservation = SpoolReservation.acquire(tmp_path / 'spool', time.monotonic() + 2)
    class Channel:
        def recv_command(self): return {'type': 'unknown'}
    try:
        with pytest.raises(ValueError):
            render(Channel(), ResourceBudget(reservation.directory, reservation.directory_fd))
    finally:
        reservation.release()


@pytest.mark.parametrize('name', [None, 'Short', 'é', 'A' * 600, '😀' * 600])
def test_fresh_isolated_helper_prepares_exact_complete_mime(tmp_path, name):
    async def run():
        async with AdmissionManager().operation('foreground', AccountPrincipal(41, True, 1)):
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                async def work():
                    session = await operation.start_helper('notification')
                    projection = Projection(operation, session)
                    for key, value in (
                        ('display_tz', 'America/Los_Angeles'),
                        ('email_to', 'invalid\nheader' if name is None else 'Group: a@example.com, a@example.com;'),
                        ('email_from', 'invalid\nheader' if name is None else 'Odograph <from@example.com>'),
                        ('app_url', 'https://example.invalid/'),
                        ('smtp_host', 'smtp.invalid'), ('smtp_username', ''), ('smtp_password', ''),
                        ('smtp_security', 'none'),
                    ):
                        await projection.literal(key, value)
                    await projection.timezone_paths()
                    reply = await session.request({'type':'initialize','now':'2026-10-01T17:00:00+00:00',
                        'hour':9,'port':25,'tls_insecure':False})
                    assert reply == {}
                    assert await session.request({'type':'quarter_bounds','hour':9}) == {'quarter_start':'2026-10-01T09:00:00-07:00'}
                    if name is None:
                        assert await session.request({'type':'render'}) == {'due_count':0,'artifacts':None}
                        await session.finish_input()
                        assert operation.process.returncode == 0
                        assert not (operation.directory / 'mail-mime').exists()
                        return
                    await projection.literal('vehicle_name', name)
                    await session.send_command({'type':'vehicle','id':1})
                    reply = await session.request({'type':'render'})
                    await session.finish_input()
                    assert operation.process.returncode == 0 and reply['due_count'] == 1
                    body = _render('quarterly_odometer.txt',vehicles=name,noun='vehicle',
                        settings_url='https://example.invalid//settings')
                    expected = EmailMessage()
                    expected['From'] = 'Odograph <from@example.com>'
                    expected['To'] = 'Group: a@example.com, a@example.com;'
                    expected['Subject'] = 'Odograph: log an odometer reading'
                    expected.set_content(body)
                    parsed = BytesParser(policy=policy.default).parsebytes(expected.as_bytes())
                    for key, international in (('mime',False),('mime_utf8',True)):
                        with operation.budget.open(reply['artifacts'][key], 'rb') as source:
                            actual = source.read()
                        expected_policy = parsed.policy.clone(utf8=True) if international else parsed.policy
                        assert actual == parsed.as_bytes(policy=expected_policy, unixfrom=False).replace(b'\n',b'\r\n')
                    with operation.budget.open(reply['artifacts']['envelope'], 'rb') as source:
                        assert json.load(source) == {'sender':'from@example.com',
                            'recipients':['a@example.com','a@example.com'],'international':False}
                await operation.perform(work)
                await operation.finish_preparation()
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(run())


def test_malformed_header_fails_and_helper_is_reaped(tmp_path):
    async def run():
        async with AdmissionManager().operation('foreground', AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                async def work():
                    session = await operation.start_helper('notification')
                    projection = Projection(operation,session)
                    for key,value in (('display_tz','UTC'),('email_to','invalid\nheader'),
                        ('email_from','from@example.com'),('app_url',''),('smtp_host','invalid'),
                        ('smtp_username',''),('smtp_password',''),('smtp_security','none')):
                        await projection.literal(key,value)
                    await projection.timezone_paths()
                    await session.request({'type':'initialize','now':'2026-10-01T17:00:00+00:00',
                        'hour':9,'port':25,'tls_insecure':False})
                    await projection.literal('vehicle_name','Car')
                    await session.send_command({'type':'vehicle','id':1})
                    await session.request({'type':'render'})
                with pytest.raises(ValueError):
                    await operation.perform(work)
            assert operation.process.returncode is not None and operation.closed
    asyncio.run(run())


@pytest.mark.parametrize('kind',['weekly_nudge','monthly_summary','filing_reminder'])
@pytest.mark.parametrize('url',['','https://example.invalid/'+'é😀/'*20000])
def test_complete_digest_body_matches_existing_template(renderer,kind,url):
    from datetime import datetime, date
    from app.digest_summary import DigestSummary
    from app.rates import YearRate
    from app.formatting import format_miles, format_usd
    from app.email_digest import MONTH_ABBR
    text(renderer,'timezone_paths',json.dumps(__import__('zoneinfo').TZPATH))
    text(renderer,'display_tz','America/Los_Angeles')
    text(renderer,'filing_mmdd','01-15')
    text(renderer,'app_url',url)
    renderer.initialize({'now':'2026-10-01T17:00:00+00:00','port':25,'tls_insecure':False})
    bounds = renderer.email_bounds(kind,9)
    if kind == 'filing_reminder':
        bounds = renderer.history_bounds()
    if kind == 'weekly_nudge':
        renderer.weekly_count = 257
        expected = _render('weekly_nudge.txt',count=257,noun='trips',review_url=url+'/review' if url else '')
        subject = 'Odograph: weekly unclassified-trip digest'
    else:
        start,end = date.fromisoformat(bounds['start']),date.fromisoformat(bounds['end'])
        tz = renderer.now.tzinfo
        rows = [(datetime.combine(start,datetime.min.time(),tzinfo=tz),'business',None,1e20),
            (datetime.combine(start,datetime.min.time(),tzinfo=tz),'business',None,3.5),
            (datetime.combine(end,datetime.min.time(),tzinfo=tz),'unclassified','not_my_vehicle',17.0),
            (datetime.combine(end,datetime.min.time(),tzinfo=tz),'business','not_deductible',1609.344)]
        renderer.history_rows([[t.isoformat(),c,e,d.hex()] for t,c,e,d in rows])
        text(renderer,'rate','0.56'); text(renderer,'rate_h2','0.625')
        renderer.rate({'year':bounds['year'],'h2':True,'month':7})
        renderer.history_finish()
        oracle = DigestSummary(); oracle.fold(rows,tz,start,end)
        oracle.finish({bounds['year']:YearRate(.56,.625,7)},bounds['year'])
        assert renderer.summary.business_m.hex() == oracle.business_m.hex()
        assert renderer.summary.total_deduction.hex() == oracle.total_deduction.hex()
        context = dict(business_mi=format_miles(oracle.business_m),nondeductible_mi=format_miles(oracle.nondeductible_m),
            deduction=format_usd(oracle.total_deduction))
        if kind == 'monthly_summary':
            context.update(month_label=f"{MONTH_ABBR[bounds['month']]} {bounds['year']}",unclassified=1,
                report_url=url+f"/report/range?from={bounds['start']}&to={bounds['end']}" if url else '')
            subject = f"Odograph: {MONTH_ABBR[bounds['month']]} summary"
        else:
            context.update(year=bounds['year'],report_url=url+f"/report/{bounds['year']}" if url else '',
                export_url=url+f"/report/{bounds['year']}/export" if url else '')
            subject = f"Odograph: {bounds['year']} filing reminder"
        expected = _render(kind+'.txt',**context)
    assert renderer.email_body() == subject
    with renderer.budget.open('mail-body','rb') as source:
        assert source.read() == expected.encode('utf8')


def test_weekly_zero_count_skips_invalid_headers(renderer):
    renderer.kind = 'weekly_nudge'; renderer.weekly_count = 0
    text(renderer,'email_to','invalid\nheader')
    assert renderer.prepare_email() == {'should_send':False,'artifacts':None}
    assert not (renderer.budget.directory/'mail-body').exists()


def test_filing_invalid_format_is_only_kind_schedule_failure(renderer):
    text(renderer,'timezone_paths',json.dumps(__import__('zoneinfo').TZPATH))
    text(renderer,'display_tz','UTC'); text(renderer,'filing_mmdd','bad'*100000)
    renderer.initialize({'now':'2026-10-01T17:00:00+00:00','port':25,'tls_insecure':False})
    assert renderer.email_bounds('weekly_nudge',18)['period_end'] == '2026-09-27T18:00:00+00:00'
    with pytest.raises(ValueError): renderer.email_bounds('filing_reminder',9)
    assert renderer.email_bounds('monthly_summary',9)['period_end'] == '2026-10-01T09:00:00+00:00'


@pytest.mark.parametrize('kind',['weekly_nudge','monthly_summary','filing_reminder'])
def test_fresh_helper_produces_complete_digest_mime(tmp_path,kind):
    async def run():
        async with AdmissionManager().operation('foreground',AccountPrincipal(41,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def work():
                    session = await operation.start_helper('notification')
                    projection = Projection(operation,session)
                    for key,value in (('display_tz','UTC'),('filing_mmdd','01-15'),
                        ('app_url','https://example.invalid/'),('email_to','to@example.com'),
                        ('email_from','from@example.com'),('smtp_host','smtp.invalid'),
                        ('smtp_username',''),('smtp_password',''),('smtp_security','none')):
                        await projection.literal(key,value)
                    await projection.timezone_paths()
                    await session.request({'type':'initialize','now':'2026-10-01T17:00:00+00:00',
                        'port':25,'tls_insecure':False})
                    bounds = await session.request({'type':'email_bounds','kind':kind,'hour':9})
                    if kind == 'filing_reminder':
                        bounds = await session.request({'type':'history_bounds'})
                    if kind == 'weekly_nudge':
                        await session.send_command({'type':'weekly_count','count':1})
                        body = _render('weekly_nudge.txt',count=1,noun='trip',review_url='https://example.invalid//review')
                        subject = 'Odograph: weekly unclassified-trip digest'
                    else:
                        await session.request({'type':'history_finish'})
                        if kind == 'monthly_summary':
                            body = _render('monthly_summary.txt',month_label='Sep 2026',business_mi='0.0',
                                nondeductible_mi='',deduction='--',unclassified=0,
                                report_url='https://example.invalid//report/range?from=2026-09-01&to=2026-09-30')
                            subject = 'Odograph: Sep summary'
                        else:
                            body = _render('filing_reminder.txt',year=2025,business_mi='0.0',nondeductible_mi='',
                                deduction='--',report_url='https://example.invalid//report/2025',
                                export_url='https://example.invalid//report/2025/export')
                            subject = 'Odograph: 2025 filing reminder'
                    result = await session.request({'type':'render_email'})
                    await session.finish_input()
                    assert result['should_send'] and operation.process.returncode == 0
                    expected = EmailMessage(); expected['From']='from@example.com'; expected['To']='to@example.com'
                    expected['Subject']=subject; expected.set_content(body)
                    with operation.budget.open(result['artifacts']['mime'],'rb') as source:
                        assert source.read() == expected.as_bytes().replace(b'\n',b'\r\n')
                await operation.perform(work)
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(run())


def literal_text(renderer,key,value,frame=1):
    raw=value.encode('utf8','surrogatepass')
    class Channel:
        def __init__(self): self.position=0
        def recv_text(self):
            chunk=raw[self.position:self.position+frame]
            self.position+=len(chunk)
            return chunk
    renderer.text(key,len(raw),Channel(),'utf8',True,True)


def test_literal_refs_survive_capture_without_changing_stored_database_decoding(renderer):
    value='normal😀\ud800\udc80\udfff'*20000
    literal_text(renderer,'app_url',value,65519)
    assert renderer.refs['app_url'][2] == 'surrogatepass' and renderer.whole('app_url') == value
    with pytest.raises(UnicodeDecodeError):
        raw=value.encode('utf8','surrogatepass')
        class Channel:
            def recv_text(self): return raw
        renderer.text('email_to',len(raw),Channel())
    assert 'email_to' not in renderer.refs


@pytest.mark.parametrize('kind',['weekly_nudge','quarterly_odometer'])
def test_zero_due_defers_all_malformed_operator_literals(renderer,kind):
    for key in ('app_url','email_from','smtp_host','smtp_username','smtp_password','smtp_security'):
        literal_text(renderer,key,'bad\n\ud800\udc80')
    renderer.initialized=True
    if kind=='weekly_nudge':
        renderer.kind=kind; renderer.weekly_count=0
        result=renderer.prepare_email()
        assert result=={'should_send':False,'artifacts':None}
    else:
        assert renderer.prepare()=={'due_count':0,'artifacts':None}
    assert not (renderer.budget.directory/'mail-config').exists()


@pytest.mark.parametrize('sender,recipient,url',[
    ('bad\n\ud800','to@example.com','\ud800'),
    ('\ud800@example.com','to@example.com',''),
    ('from@example.com','bad\n\ud800','\ud800'),
    ('from@example.com','\ud800@example.com',''),
    ('from@example.com','to@example.com','https://example.invalid/\ud800'),
    ('from@example.com','to@example.com','https://example.invalid/\udc80'),
])
def test_actual_literal_use_matches_legacy_header_then_body_failure(renderer,sender,recipient,url):
    from app.mailer import Mailer
    body=_render('quarterly_odometer.txt',vehicles='Car',noun='vehicle',settings_url=url+'/settings' if url else '')
    with pytest.raises((ValueError,UnicodeEncodeError)) as expected:
        Mailer('',0,'','','none',False,sender,recipient).compose('Odograph: log an odometer reading',body)
    for key,value in (('email_from',sender),('email_to',recipient),('app_url',url),
        ('smtp_host','invalid'),('smtp_username',''),('smtp_password',''),('smtp_security','none')):
        literal_text(renderer,key,value)
    renderer.initialized=True
    text(renderer,'vehicle_name','Car'); renderer.vehicle(1)
    with pytest.raises(type(expected.value)):
        renderer.prepare()


@pytest.mark.parametrize('sender,recipient',[
    ('\udc80@example.com','to@example.com'),
    ('from@example.com','\udc80@example.com'),
    ('Name \udcff <from@example.com>','Name \udc80 <to@example.com>'),
])
def test_accepted_surrogateescape_headers_match_complete_legacy_mime_and_envelope(renderer,sender,recipient):
    from app.mailer import Mailer
    from app.mime_preparation import envelope
    url='https://example.invalid/'
    for key,value in (('email_from',sender),('email_to',recipient),('app_url',url),
        ('smtp_host','invalid'),('smtp_username',''),('smtp_password','\ud800\udc80'),('smtp_security','none')):
        literal_text(renderer,key,value)
    renderer.initialized=True; renderer.port=25; renderer.tls_insecure=False
    text(renderer,'vehicle_name','Car'); renderer.vehicle(1)
    result=renderer.prepare()
    body=_render('quarterly_odometer.txt',vehicles='Car',noun='vehicle',settings_url=url+'/settings')
    message=Mailer('',0,'','','none',False,sender,recipient).compose('Odograph: log an odometer reading',body)
    parsed=BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    for key,utf8 in (('mime',False),('mime_utf8',True)):
        with renderer.budget.open(result['artifacts'][key],'rb') as source:
            assert source.read() == parsed.as_bytes(policy=parsed.policy.clone(utf8=True) if utf8 else parsed.policy).replace(b'\n',b'\r\n')
    with renderer.budget.open(result['artifacts']['envelope'],'rb') as source:
        actual=json.load(source)
    env_sender,env_recipients,international=envelope(parsed)
    assert actual==dict(sender=env_sender,recipients=env_recipients,international=international)
    with renderer.budget.open(result['artifacts']['config'],'rb') as source:
        assert json.load(source)['password']==json.loads(json.dumps('\ud800\udc80'))


def test_actual_surrogate_config_use_matches_original_transport_phase(renderer,monkeypatch):
    from types import SimpleNamespace
    from app.mailer import Mailer
    from app.smtp_helper import smtp_transport,prepared_transport
    from contextlib import ExitStack
    for key,value in (('email_from','from@example.com'),('email_to','to@example.com'),('app_url',''),
        ('smtp_host','invalid'),('smtp_username',''),('smtp_password','\ud800'),('smtp_security','\ud800')):
        literal_text(renderer,key,value)
    renderer.initialized=True; renderer.port=25; renderer.tls_insecure=False
    text(renderer,'vehicle_name','Car'); renderer.vehicle(1)
    result=renderer.prepare()
    with renderer.budget.open(result['artifacts']['config'],'rb') as source:
        config=SimpleNamespace(**json.load(source))
    message=Mailer('',0,'','','none',False,'from@example.com','to@example.com').compose('Test','Body')
    monkeypatch.setattr('app.smtp_helper.ssl.create_default_context',lambda:object())
    old_state,new_state={},{}
    with pytest.raises(ValueError): smtp_transport(config,message,old_state)
    with ExitStack() as stack:
        streams=[stack.enter_context(renderer.budget.open(result['artifacts'][key],'rb')) for key in ('mime','mime_utf8')]
        with pytest.raises(ValueError): prepared_transport(config,{},*(s.fileno() for s in streams),new_state)
    assert old_state==new_state=={'phase':'connect'}
