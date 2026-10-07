"""Bounded digest email projections under an admitted preparation owner."""
from __future__ import annotations

from contextlib import aclosing
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import math
from uuid import uuid4
import zoneinfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.capacity import owned_thread, await_completion
from app.preparation import FRAME_BYTES, _settle

TEXT_CHARS = 16380
KINDS = ('weekly_nudge', 'monthly_summary', 'filing_reminder', 'quarterly_odometer')
FLAGS = ('email_weekly_nudge', 'email_monthly_summary', 'email_filing_reminder', 'email_odometer_reminder')


@dataclass(frozen=True)
class EmailTurnSelection:
    kind: str | None
    index: int | None = None
    ready: bool = False
    cursor: int | None = None


@dataclass(frozen=True)
class QuarterlyJob:
    operation: object
    session: object
    quarter_start: datetime
    hour: int


@dataclass(frozen=True)
class EmailJob:
    operation: object
    session: object
    kind: str
    period_end: datetime
    hour: int
    bounds: dict


@dataclass(frozen=True)
class PreparedEmail:
    should_send: bool
    artifacts: dict | None


@dataclass(frozen=True)
class QuarterlyPrepared:
    due_count: int
    artifacts: dict | None


@dataclass(frozen=True)
class CapturedEmailSettings:
    operation: object
    references: dict
    enabled: tuple[bool, ...]
    nudge_weekly_hour: int
    odometer_reminder_hour: int
    digest_hour: int
    now: datetime
    port: int
    tls_insecure: bool


class Projection:
    def __init__(self, operation, session=None):
        self.operation, self.session = operation, session

    async def timeout(self, conn):
        self.operation.check()
        await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                           (str(min(15000, self.operation.remaining_ms())),))

    async def scalar(self, conn, query, params=()):
        await self.timeout(conn)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchone()

    async def text(self, conn, key, size, field, *, keep=True):
        if field not in ('display_tz', 'email_to', 'email_filing_reminder_mmdd', 'ntfy_topic'):
            raise ValueError('unknown notification preference')
        codec = conn.info.encoding
        if codec == 'ascii':
            raise TypeError('notification settings require decoded text')
        await self.session.send_command({'type': 'text', 'key': key, 'size': size, 'encoding': codec,'keep':keep})
        position, sent = 1, 0
        while sent < size:
            row = await self.scalar(conn,
                f"SELECT substring(convert_to({field},current_setting('client_encoding')) from %s for %s) AS value "
                'FROM account_settings WHERE account_id=%s', (position, FRAME_BYTES, account_id(conn)))
            if row is None or not row['value']:
                raise RuntimeError('notification preference changed while locked')
            raw = row['value']
            if len(raw) > FRAME_BYTES or sent + len(raw) > size:
                raise RuntimeError('invalid notification preference length')
            await self.session.send_text(raw)
            sent += len(raw)
            position += len(raw)

    async def literal(self, key, value):
        size = 0
        for i in range(0,len(value),TEXT_CHARS):
            self.operation.check()
            size += len(value[i:i + TEXT_CHARS].encode('utf8','surrogatepass'))
            await asyncio.sleep(0)
        await self.session.send_command({'type':'text','key':key,'size':size,'encoding':'utf8','literal':True})
        for i in range(0, len(value), TEXT_CHARS):
            self.operation.check()
            await self.session.send_text(value[i:i + TEXT_CHARS].encode('utf8','surrogatepass'))

    async def timezone_paths(self):
        def chunks():
            yield b'['
            for index, path in enumerate(zoneinfo.TZPATH):
                if index:
                    yield b','
                yield b'"'
                for offset in range(0, len(path), 4096):
                    yield json.dumps(path[offset:offset + 4096], ensure_ascii=True)[1:-1].encode('ascii')
                yield b'"'
            yield b']'
        size = 0
        for chunk in chunks():
            self.operation.check()
            size += len(chunk)
            await asyncio.sleep(0)
        await self.session.send_command({'type': 'text', 'key': 'timezone_paths', 'size': size})
        for chunk in chunks():
            await self.session.send_text(chunk)


async def select_email_turn(conn, cursor=None, *, operation) -> EmailTurnSelection:
    """Caller keeps this capture transaction open through settings capture."""
    row = await Projection(operation).scalar(conn,
        'SELECT ' + ','.join(FLAGS) + ", octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size "
        'FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    if row is None:
        raise RuntimeError('account preferences are missing')
    if not row['to_size']:
        return EmailTurnSelection(None)
    start = cursor or 0
    for index in range(start, len(KINDS)):
        if row[FLAGS[index]]:
            ready = any(row[flag] for flag in FLAGS[index + 1:])
            return EmailTurnSelection(KINDS[index], index, ready, index + 1 if ready else None)
    return EmailTurnSelection(None)


async def capture_email_job(conn, operation, config, kind, *, now=None) -> EmailJob:
    if kind not in KINDS:
        raise ValueError("unknown email kind")
    session = await operation.start_helper('notification')
    projection = Projection(operation, session)
    row = await projection.scalar(conn,
        "SELECT octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size, "
        "octet_length(convert_to(ntfy_topic,current_setting('client_encoding'))) AS ntfy_size, "
        "octet_length(convert_to(email_filing_reminder_mmdd,current_setting('client_encoding'))) AS filing_size, odometer_reminder_hour, nudge_weekly_hour, email_digest_hour "
        'FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    if row is None:
        raise RuntimeError('account preferences are missing')
    await projection.text(conn, 'display_tz', row['tz_size'], 'display_tz')
    await projection.text(conn, 'email_to', row['to_size'], 'email_to')
    await projection.text(conn, 'ntfy_topic', row['ntfy_size'], 'ntfy_topic',keep=False)
    await projection.text(conn, 'filing_mmdd', row['filing_size'], 'email_filing_reminder_mmdd')
    await projection.timezone_paths()
    await session.request({'type': 'initialize',
        'now': now.isoformat() if now is not None else None,
        'port': config.smtp_port, 'tls_insecure': config.smtp_tls_insecure})
    for key, value in (
        ('app_url', config.app_url), ('email_from', config.email_from),
        ('smtp_host', config.smtp_host), ('smtp_username', config.smtp_username),
        ('smtp_password', config.smtp_password), ('smtp_security', config.smtp_security),
    ):
        await projection.literal(key, value)
    hour = row[_hour_field(kind)]
    bounds = await _email_bounds(session, kind, hour)
    return EmailJob(operation, session, kind, datetime.fromisoformat(bounds['period_end']), hour, bounds)


async def capture_quarterly_job(conn, operation, config, *, now=None) -> QuarterlyJob:
    job = await capture_email_job(conn, operation, config, 'quarterly_odometer', now=now)
    return QuarterlyJob(operation, job.session, job.period_end, job.hour)


async def capture_email_settings(conn, operation, config, *, now=None) -> CapturedEmailSettings:
    """Capture once for a legacy sweep; caller retains this grant until it ends."""
    session = await operation.start_helper('notification')
    projection = Projection(operation, session)
    row = await projection.scalar(conn,
        'SELECT ' + ','.join(FLAGS) + ', nudge_weekly_hour, odometer_reminder_hour, email_digest_hour, '
        "octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size, "
        "octet_length(convert_to(ntfy_topic,current_setting('client_encoding'))) AS ntfy_size, "
        "octet_length(convert_to(email_filing_reminder_mmdd,current_setting('client_encoding'))) AS filing_size "
        'FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    if row is None:
        raise RuntimeError('account preferences are missing')
    for key, size, field in (('display_tz',row['tz_size'],'display_tz'),
        ('email_to',row['to_size'],'email_to'), ('filing_mmdd',row['filing_size'],'email_filing_reminder_mmdd')):
        await projection.text(conn,key,size,field)
    await projection.text(conn,'ntfy_topic',row['ntfy_size'],'ntfy_topic',keep=False)
    await projection.timezone_paths()
    await session.request({'type':'initialize',
        'now':now.isoformat() if now is not None else None,'port':config.smtp_port,'tls_insecure':config.smtp_tls_insecure})
    for key, value in (
        ('app_url',config.app_url), ('email_from',config.email_from), ('smtp_host',config.smtp_host),
        ('smtp_username',config.smtp_username), ('smtp_password',config.smtp_password), ('smtp_security',config.smtp_security),
    ):
        await projection.literal(key,value)
    captured = await session.request({'type':'capture'})
    await session.finish_input()
    return CapturedEmailSettings(operation, captured['references'], tuple(row[flag] for flag in FLAGS),
        row['nudge_weekly_hour'], row['odometer_reminder_hour'], row['email_digest_hour'],
        datetime.fromisoformat(captured['now']),
        config.smtp_port, config.smtp_tls_insecure)


async def replay_email_job(captured, operation, kind) -> EmailJob:
    if kind not in KINDS:
        raise ValueError("unknown email kind")
    """Copy complete initial metadata into a fresh, independently charged turn."""
    session = await operation.start_helper('notification')
    captured.operation.reservation.validate()
    source = await _open_captured(captured)
    try:
        for key in ('display_tz','email_to','timezone_paths','app_url','email_from',
                    'smtp_host','smtp_username','smtp_password','smtp_security','filing_mmdd'):
            ref = captured.references[key]
            offset,left = ref[:2]
            command = {'type':'text','key':key,'size':left}
            if len(ref) == 3:
                if ref[2] != 'surrogatepass':
                    raise ValueError('invalid captured literal codec')
                command.update(encoding='utf8',literal=True)
            await session.send_command(command)
            await owned_thread(source.seek,offset)
            while left:
                operation.check()
                chunk = await owned_thread(source.read,min(left,FRAME_BYTES))
                if not chunk:
                    raise RuntimeError('incomplete captured notification settings')
                await session.send_text(chunk)
                left -= len(chunk)
    finally:
        await _close_captured(source)
    await session.request({'type':'initialize',
        'now':captured.now.isoformat(),'port':captured.port,'tls_insecure':captured.tls_insecure})
    hour = (captured.nudge_weekly_hour if kind == 'weekly_nudge' else
            captured.odometer_reminder_hour if kind == 'quarterly_odometer' else captured.digest_hour)
    bounds = await _email_bounds(session, kind, hour)
    return EmailJob(operation,session,kind,datetime.fromisoformat(bounds['period_end']),hour,bounds)


async def replay_quarterly_job(captured, operation) -> QuarterlyJob:
    job = await replay_email_job(captured, operation, 'quarterly_odometer')
    return QuarterlyJob(operation,job.session,job.period_end,job.hour)


def _hour_field(kind):
    return ('nudge_weekly_hour' if kind == 'weekly_nudge' else
            'odometer_reminder_hour' if kind == 'quarterly_odometer' else 'email_digest_hour')


async def _email_bounds(session, kind, hour):
    try:
        return await session.request({'type':'email_bounds','kind':kind,'hour':hour})
    except (ValueError, OverflowError) as exc:
        exc.preparation_stage = 'schedule'
        raise


async def _close_captured(source):
    task = asyncio.create_task(owned_thread(source.close))
    _, cancelled = await _settle(task)
    if cancelled:
        raise asyncio.CancelledError


async def _open_captured(captured):
    task = asyncio.create_task(owned_thread(captured.operation.budget.open,'notification-text','rb'))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        source, _ = await _settle(task)
        await _close_captured(source)
        raise


async def _quarter_bounds(session, hour):
    try:
        return await session.request({'type':'quarter_bounds','hour':hour})
    except (ValueError, OverflowError) as exc:
        exc.preparation_stage = 'schedule'
        raise


async def quarterly_preferences_current(conn, job) -> bool:
    projection = Projection(job.operation, job.session)
    row = await projection.scalar(conn,
        "SELECT octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size, "
        'email_odometer_reminder, odometer_reminder_hour FROM account_settings WHERE account_id=%s FOR SHARE',
        (account_id(conn),))
    if row is None or not row['to_size'] or not row['email_odometer_reminder'] or row['odometer_reminder_hour'] != job.hour:
        return False
    await projection.text(conn, 'current_display_tz', row['tz_size'], 'display_tz')
    await projection.text(conn, 'current_email_to', row['to_size'], 'email_to')
    return (await job.session.request({'type': 'preferences'}))['current']


async def prepare_quarterly_message(conn, job) -> QuarterlyPrepared:
    projection = Projection(job.operation, job.session)
    query = f"""SELECT v.id, octet_length(encoded.value) AS size,
        NOT EXISTS (SELECT 1 FROM odometer_readings r WHERE r.account_id=v.account_id
            AND r.vehicle_id=v.id AND r.recorded_at >= %s) AS due,
        parts.position, substring(encoded.value from parts.position for {FRAME_BYTES}) AS value
        FROM vehicles v CROSS JOIN LATERAL (VALUES (
            convert_to(v.name,current_setting('client_encoding')))) AS encoded(value)
        CROSS JOIN LATERAL generate_series(
            1,GREATEST(octet_length(encoded.value),1),{FRAME_BYTES}) AS parts(position)
        WHERE v.account_id=%s AND v.active
        ORDER BY v.id,parts.position"""
    await projection.timeout(conn)
    active, left, position, due = None, 0, 1, False
    async with conn.cursor(row_factory=dict_row) as cursor:
        async with aclosing(cursor.stream(query, (job.quarter_start, account_id(conn)), size=1)) as stream:
            async for row in stream:
                job.operation.check()
                if row['id'] != active:
                    if left:
                        raise RuntimeError('incomplete notification name')
                    if active is not None:
                        await job.session.send_command({'type': 'vehicle', 'id': active, 'due': due})
                    active, left = row['id'], row['size']
                    due = row['due']
                    position = 1
                    await job.session.send_command({'type': 'text', 'key': 'vehicle_name', 'size': left,
                        'encoding': conn.info.encoding, 'keep': due})
                if row['position'] != position:
                    raise RuntimeError('invalid notification name position')
                raw = row['value']
                if len(raw) > FRAME_BYTES or len(raw) > left:
                    raise RuntimeError('invalid notification name length')
                if raw:
                    await job.session.send_text(raw)
                left -= len(raw)
                position += len(row['value'])
                del row, raw
    if left:
        raise RuntimeError('incomplete notification name')
    if active is not None:
        await job.session.send_command({'type': 'vehicle', 'id': active, 'due': due})
    result = await job.session.request({'type': 'render'})
    await job.session.finish_input()
    return QuarterlyPrepared(result['due_count'], result.get('artifacts'))


async def email_preferences_current(conn, job) -> bool:
    projection = Projection(job.operation, job.session)
    flag = FLAGS[KINDS.index(job.kind)]
    hour_field = _hour_field(job.kind)
    row = await projection.scalar(conn,
        "SELECT octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size, "
        "octet_length(convert_to(email_filing_reminder_mmdd,current_setting('client_encoding'))) AS filing_size, "
        f'{flag}, {hour_field} FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    if row is None or not row['to_size'] or not row[flag] or row[hour_field] != job.hour:
        return False
    await projection.text(conn,'current_display_tz',row['tz_size'],'display_tz')
    await projection.text(conn,'current_email_to',row['to_size'],'email_to')
    if job.kind == 'filing_reminder':
        await projection.text(conn,'current_filing_mmdd',row['filing_size'],'email_filing_reminder_mmdd')
    return (await job.session.request({'type':'preferences','filing':job.kind == 'filing_reminder'}))['current']


async def _history(conn, job):
    from app.digest_summary import DigestPreparationTimeout, PREPARATION_SECONDS, BATCH_SIZE
    from psycopg.rows import tuple_row
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PREPARATION_SECONDS
    cursor = conn.cursor(name='notification_' + uuid4().hex, row_factory=tuple_row,
                         scrollable=False, withhold=False)
    original_timeout = None

    async def ceiling():
        job.operation.check()
        left = math.floor((deadline-loop.time())*1000)
        if left <= 0:
            raise DigestPreparationTimeout('digest preparation deadline expired')
        timeout = min(left,job.operation.remaining_ms())
        if original_timeout:
            timeout = min(timeout,original_timeout)
        await conn.execute("SELECT set_config('statement_timeout',%s,true)",(f'{timeout}ms',))

    try:
        try:
            async with asyncio.timeout_at(deadline):
                cur = await conn.execute("SELECT extract(epoch FROM current_setting('statement_timeout')::interval)*1000")
                original_timeout = int((await cur.fetchone())[0])
                await ceiling()
                await cursor.execute(
                    'SELECT started_at,category,exclusion,COALESCE(distance_snapped_m,distance_m) '
                    'FROM trips WHERE account_id=%s AND started_at>=%s AND started_at<%s '
                    'ORDER BY started_at ASC,id ASC',
                    (account_id(conn),datetime.fromisoformat(job.bounds['range_start']),
                     datetime.fromisoformat(job.bounds['range_end'])))
                while True:
                    await ceiling()
                    rows = await cursor.fetchmany(BATCH_SIZE)
                    if not rows:
                        break
                    # Only fixed scalar projections cross into the helper; float.hex preserves every bit.
                    await job.session.send_command({'type':'history_rows','rows':[
                        [started.isoformat(),category,exclusion,distance.hex()]
                        for started,category,exclusion,distance in rows]})
                    del rows
                await ceiling()
                query = f"""WITH selected AS (
                    SELECT year,rate_per_mi,rate_h2_per_mi,h2_start_month FROM mileage_rates
                    WHERE account_id=%s AND year<=%s ORDER BY year DESC LIMIT 1)
                    SELECT year,h2_start_month,rate_h2_per_mi IS NOT NULL AS h2,
                    encoded.key,octet_length(encoded.value) AS size,parts.position,
                    substring(encoded.value from parts.position for {FRAME_BYTES}) AS value
                    FROM selected CROSS JOIN LATERAL (VALUES
                    ('rate',convert_to(rate_per_mi::text,current_setting('client_encoding'))),
                    ('rate_h2',convert_to(rate_h2_per_mi::text,current_setting('client_encoding')))) AS encoded(key,value)
                    CROSS JOIN LATERAL generate_series(1,GREATEST(octet_length(encoded.value),1),{FRAME_BYTES}) AS parts(position)
                    WHERE encoded.value IS NOT NULL ORDER BY encoded.key,parts.position"""
                active,left,position,rate = None,0,1,None
                async with conn.cursor(row_factory=dict_row) as rate_cursor:
                    async with aclosing(rate_cursor.stream(query,(account_id(conn),job.bounds['year']),size=1)) as stream:
                        async for row in stream:
                            job.operation.check()
                            if row['key'] != active:
                                if left:
                                    raise RuntimeError('incomplete notification rate')
                                active,left,position = row['key'],row['size'],1
                                rate = {'type':'rate','year':row['year'],'h2':row['h2'],'month':row['h2_start_month']}
                                await job.session.send_command({'type':'text','key':active,'size':left,'encoding':conn.info.encoding})
                            raw = row['value']
                            if row['position'] != position or not raw or len(raw)>FRAME_BYTES or len(raw)>left:
                                raise RuntimeError('incomplete notification rate')
                            await job.session.send_text(raw)
                            left -= len(raw)
                            position += len(raw)
                            del row,raw
                if left:
                    raise RuntimeError('incomplete notification rate')
                if rate is not None:
                    await job.session.send_command(rate)
                await job.session.request({'type':'history_finish'})
                await ceiling()
                await conn.execute("SELECT set_config('statement_timeout',%s,true)",(f'{original_timeout}ms',))
        except TimeoutError as exc:
            raise DigestPreparationTimeout('digest preparation deadline expired') from exc
    finally:
        await await_completion(asyncio.create_task(cursor.close()))
    if loop.time() >= deadline:
        raise DigestPreparationTimeout('digest preparation deadline expired')


async def prepare_email_message(conn, job) -> PreparedEmail:
    if job.kind == 'quarterly_odometer':
        prepared = await prepare_quarterly_message(conn,QuarterlyJob(job.operation,job.session,job.period_end,job.hour))
        return PreparedEmail(bool(prepared.due_count),prepared.artifacts)
    if job.kind == 'weekly_nudge':
        row = await Projection(job.operation).scalar(conn,
            "SELECT count(*) AS count FROM trips WHERE account_id=%s AND category='unclassified' "
            'AND started_at>=%s AND started_at<%s',
            (account_id(conn),datetime.fromisoformat(job.bounds['window_start']),job.period_end))
        await job.session.send_command({'type':'weekly_count','count':row['count']})
    else:
        if job.kind == 'filing_reminder':
            job.bounds.update(await job.session.request({'type':'history_bounds'}))
        await _history(conn,job)
    result = await job.session.request({'type':'render_email'})
    await job.session.finish_input()
    return PreparedEmail(result['should_send'],result.get('artifacts'))
