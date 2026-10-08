"""Admitted NTFY projections, lifecycle locks and prepared descriptor delivery."""
from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
import json
import os

from psycopg.rows import dict_row

from app.account_context import AccountPool, account_id
from app.account_work import external_account_work
from app.db import NUDGE_ADVISORY_LOCK_KEY, ODOMETER_REMINDER_ADVISORY_LOCK_KEY
from app.notification_preparation import Projection
from app.ntfy_cookies import cookie_chunks, cookies_in_place
from app.ntfy_helper import ENVIRONMENT_KEYS
from app.ntfy_supervisor import send_prepared
from app.preparation import FRAME_BYTES, PreparationOperation
from app.prepared_ntfy import PreparedNtfy
from app.worker import TurnOutcome


@dataclass(frozen=True)
class NtfyJob:
    operation: object
    session: object
    kind: str
    end: datetime
    start: datetime
    hour: int
    enabled: bool


async def capture_ntfy_job(conn, operation, config, kind, *, now=None):
    session = await operation.start_helper('ntfy')
    projection = Projection(operation, session)
    row = await projection.scalar(conn,
        "SELECT octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(ntfy_topic,current_setting('client_encoding'))) AS topic_size, "
        "octet_length(convert_to(email_to,current_setting('client_encoding'))) AS to_size, "
        "octet_length(convert_to(email_filing_reminder_mmdd,current_setting('client_encoding'))) AS filing_size, "
        'nudge_weekly_hour, odometer_reminder_hour, odometer_reminder_requested '
        'FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    if row is None:
        raise RuntimeError('account preferences are missing')
    for key, size, field, keep in (
        ('display_tz', 'tz_size', 'display_tz', True),
        ('ntfy_topic', 'topic_size', 'ntfy_topic', True),
        ('email_to', 'to_size', 'email_to', False),
        ('filing_mmdd', 'filing_size', 'email_filing_reminder_mmdd', False),
    ):
        await projection.text(conn, key, row[size], field, keep=keep)
    await projection.timezone_paths()
    hour = row['nudge_weekly_hour' if kind == 'weekly' else 'odometer_reminder_hour']
    enabled = bool(config.ntfy_url and row['topic_size'] and
                   (kind == 'weekly' or row['odometer_reminder_requested']))
    bounds = await session.request({'type':'initialize','kind':kind,'hour':hour,'enabled':enabled,
        'now':now.isoformat() if now is not None else None})
    for key, value in (('app_url', config.app_url), ('url', config.ntfy_url),
                       ('token', config.ntfy_token), ('username', config.ntfy_username),
                       ('password', config.ntfy_password)):
        await projection.literal(key, value)
    return NtfyJob(operation, session, kind, datetime.fromisoformat(bounds['end']),
                   datetime.fromisoformat(bounds['start']), hour, enabled)


async def ntfy_preferences_current(conn, job):
    projection = Projection(job.operation, job.session)
    row = await projection.scalar(conn,
        "SELECT octet_length(convert_to(display_tz,current_setting('client_encoding'))) AS tz_size, "
        "octet_length(convert_to(ntfy_topic,current_setting('client_encoding'))) AS topic_size, "
        'nudge_weekly_hour, odometer_reminder_hour, odometer_reminder_requested '
        'FROM account_settings WHERE account_id=%s FOR SHARE', (account_id(conn),))
    hour_key = 'nudge_weekly_hour' if job.kind == 'weekly' else 'odometer_reminder_hour'
    if (row is None or not row['topic_size'] or row[hour_key] != job.hour
            or (job.kind != 'weekly' and not row['odometer_reminder_requested'])):
        return False
    await projection.text(conn, 'current_display_tz', row['tz_size'], 'display_tz')
    await projection.text(conn, 'current_ntfy_topic', row['topic_size'], 'ntfy_topic')
    return (await job.session.request({'type': 'preferences'}))['current']


async def _vehicles(conn, job):
    projection = Projection(job.operation, job.session)
    if conn.info.encoding == 'ascii':
        raise TypeError('notification names require decoded text')
    query = f"""SELECT v.id, octet_length(encoded.value) AS size,
        NOT EXISTS (SELECT 1 FROM odometer_readings r WHERE r.account_id=v.account_id
            AND r.vehicle_id=v.id AND r.recorded_at >= %s) AS due,
        parts.position, substring(encoded.value from parts.position for {FRAME_BYTES}) AS value
        FROM vehicles v CROSS JOIN LATERAL (VALUES (
            convert_to(v.name,current_setting('client_encoding')))) AS encoded(value)
        CROSS JOIN LATERAL generate_series(
            1,GREATEST(octet_length(encoded.value),1),{FRAME_BYTES}) AS parts(position)
        WHERE v.account_id=%s AND v.active ORDER BY v.name,v.id,parts.position"""
    await projection.timeout(conn)
    active, left, position, due = None, 0, 1, False
    async with conn.cursor(row_factory=dict_row) as cursor:
        async with aclosing(cursor.stream(query, (job.end, account_id(conn)), size=1)) as stream:
            async for row in stream:
                job.operation.check()
                if row['id'] != active:
                    if left:
                        raise RuntimeError('incomplete notification name')
                    if active is not None:
                        await job.session.send_command({'type': 'vehicle', 'id': active, 'due': due})
                    active, left, due, position = row['id'], row['size'], row['due'], 1
                    await job.session.send_command({'type': 'text', 'key': 'vehicle_name', 'size': left,
                        'encoding': conn.info.encoding, 'keep': due})
                raw = row['value']
                if row['position'] != position or len(raw) > FRAME_BYTES or len(raw) > left:
                    raise RuntimeError('invalid notification name frame')
                if raw:
                    await job.session.send_text(raw)
                left -= len(raw)
                position += len(raw)
    if left:
        raise RuntimeError('incomplete notification name')
    if active is not None:
        await job.session.send_command({'type': 'vehicle', 'id': active, 'due': due})


async def _cookie_state(job, http_client):
    # Existing jar records may be arbitrarily large. Their complete transfer
    # is charged spool output, and only the isolated child parses them.
    for cookie in cookies_in_place(http_client.cookies.jar):
        size = 0
        for chunk in cookie_chunks(cookie):
            job.operation.check()
            size += len(chunk)
        await job.session.send_command({'type': 'cookie', 'size': size})
        for chunk in cookie_chunks(cookie):
            job.operation.check()
            await job.session.send_text(chunk)
    environment = {key: os.environ[key] for key in ENVIRONMENT_KEYS if key in os.environ}
    # A fixed number of held environment values, encoded a piece at a time.
    def chunks():
        yield b'{'
        for index, (key, value) in enumerate(environment.items()):
            if index:
                yield b','
            yield json.dumps(key).encode('ascii') + b':"'
            for offset in range(0, len(value), 4096):
                yield json.dumps(value[offset:offset + 4096], ensure_ascii=True)[1:-1].encode('ascii')
            yield b'"'
        yield b'}'
    size = sum(len(raw) for raw in chunks())
    await job.session.send_command({'type': 'text', 'key': 'environment', 'size': size})
    for raw in chunks():
        await job.session.send_text(raw)


async def _delivery(conn, job, http_client):
    operation = job.operation
    projection = Projection(operation, job.session)
    lock = NUDGE_ADVISORY_LOCK_KEY if job.kind == 'weekly' else ODOMETER_REMINDER_ADVISORY_LOCK_KEY
    table = 'nudge_delivery_windows' if job.kind == 'weekly' else 'odometer_reminder_windows'
    field = 'window_ends_at' if job.kind == 'weekly' else 'quarter_starts_at'
    async with conn.transaction():
        await projection.timeout(conn)
        await conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
        if not await ntfy_preferences_current(conn, job):
            await job.session.request({'type': 'discard'})
            await job.session.finish_input()
            await operation.finish_preparation()
            return
        row = await projection.scalar(conn,
            f'SELECT 1 AS present FROM {table} WHERE account_id=%s AND {field}=%s', (account_id(conn), job.end))
        if row is not None:
            await job.session.request({'type': 'discard'})
            await job.session.finish_input()
            await operation.finish_preparation()
            return
        if job.kind == 'weekly':
            row = await projection.scalar(conn,
                "SELECT count(*) AS count FROM trips WHERE account_id=%s AND category='unclassified' "
                'AND started_at >= %s AND started_at < %s', (account_id(conn), job.start, job.end))
            count = row['count']
        else:
            await _vehicles(conn, job)
            count = (await job.session.request({'type': 'count'}))['count']
        if count:
            await _cookie_state(job, http_client)
        result = await job.session.request({'type': 'render', 'count': count})
        await job.session.finish_input()
        if result['count']:
            await send_prepared(PreparedNtfy(operation.reservation, result['artifacts']),
                http_client=http_client, before_transport=operation.finish_preparation)
        else:
            await operation.finish_preparation()
        value = result['count'] if job.kind == 'weekly' else bool(result['count'])
        value_field = 'trip_count' if job.kind == 'weekly' else 'reminded'
        await conn.execute(f'INSERT INTO {table} (account_id,{field},{value_field}) VALUES (%s,%s,%s)',
            (account_id(conn), job.end, value))


async def _run(worker, principal, kind, *, legacy, http_client):
    skipped = False
    async with PreparationOperation(spool_root=worker.config.preparation_spool_dir) as operation:
        async def run():
            nonlocal skipped
            async with external_account_work(worker.pools.control, principal.account_id):
                try:
                    pool = AccountPool(worker.pools.runtime, principal)
                    async with pool.connection() as conn:
                        async with conn.transaction():
                            job = await capture_ntfy_job(conn, operation, worker.config, kind)
                    skipped = not job.enabled
                    if job.enabled:
                        async with pool.connection() as conn:
                            await _delivery(conn, job, http_client)
                    else:
                        await job.session.request({'type': 'discard'})
                        await job.session.finish_input()
                        await operation.finish_preparation()
                except BaseException as exc:
                    await operation.backend_failure(exc)
                    raise
                finally:
                    await operation.close()
        await operation.perform(run)
    return TurnOutcome(skipped=legacy and skipped)


async def run_prepared_nudge_turn(worker, principal, cursor, *, legacy=False, http_client):
    return await _run(worker, principal, 'weekly', legacy=legacy, http_client=http_client)


async def run_prepared_odometer_turn(worker, principal, cursor, *, legacy=False, http_client):
    return await _run(worker, principal, 'quarterly', legacy=legacy, http_client=http_client)
