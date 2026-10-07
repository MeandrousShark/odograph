"""Bounded report projection from one account and shell database snapshot."""
from __future__ import annotations

from contextlib import aclosing
from datetime import date, datetime
from decimal import Decimal
import json
import zoneinfo

from psycopg.rows import dict_row

from app.account_context import account_id
from app.page import _fetch_review_count
from app.preparation import FRAME_BYTES
from app.storage import storage_status
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.ui._common import (
    _START_PLACE_NAME_SQL, _END_PLACE_NAME_SQL, _START_ADDRESS_SQL, _END_ADDRESS_SQL,
)

FETCH_ROWS = 256
TEXT_CHARS = 16380
# PostgreSQL btrim's explicit set matches Python's Unicode str.strip().
_WHITESPACE = ''.join(chr(i) for i in range(0x3100) if chr(i).isspace())
_TRIP_SCALARS = f"""id, vehicle_id, started_at, category::text AS category,
    exclusion::text AS exclusion, {DISPLAY_DISTANCE_SQL} AS display_distance_m,
    length(btrim(COALESCE(purpose,''), %s)) > 0 AS purpose_nonblank,
    has_gap, snap_status::text AS snap_status, source::text AS source"""
_DETAIL_TEXT = {
    'start_place_name': f'COALESCE(start_label, {_START_PLACE_NAME_SQL})',
    'end_place_name': f'COALESCE(end_label, {_END_PLACE_NAME_SQL})',
    'start_address': _START_ADDRESS_SQL, 'end_address': _END_ADDRESS_SQL,
    'vehicle_name': '(SELECT name FROM vehicles WHERE account_id = trips.account_id AND id = trips.vehicle_id)',
    'purpose': 'purpose', 'notes': 'notes',
}


def _timezone_path_chunks(paths):
    yield b'['
    for i, path in enumerate(paths):
        if i:
            yield b','
        yield b'"'
        for offset in range(0, len(path), 4096):
            yield json.dumps(path[offset:offset + 4096], ensure_ascii=True)[1:-1].encode('ascii')
        yield b'"'
    yield b']'


def _encode(row):
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else
            value.hex() if isinstance(value, float) else str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()}


class Projection:
    def __init__(self, operation, session):
        self.operation, self.session = operation, session
        self.text_offset = 0
        self.portal_number = 0

    async def timeout(self, conn):
        await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                           (str(min(15000, self.operation.remaining_ms())),))

    async def scalar(self, conn, query, params=()):
        await self.timeout(conn)
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchone()

    async def rows(self, conn, query, params=()):
        self.portal_number += 1
        async with conn.cursor(name=f'report_{self.portal_number}', row_factory=dict_row) as cursor:
            await self.timeout(conn)
            await cursor.execute(query, params)
            while True:
                await self.timeout(conn)
                batch = await cursor.fetchmany(FETCH_ROWS)
                if not batch:
                    return
                for row in batch:
                    self.operation.check()
                    yield row
                del row, batch

    async def text(self, conn, key, size, expression, table, predicate, params):
        """Only module-owned SQL expressions and bounded substrings enter Python."""
        size = size or 0
        ref = [self.text_offset, size]
        await self.session.send_command({'type': 'text', 'key': key, 'size': size})
        sent, position = 0, 1
        while sent < size:
            row = await self.scalar(conn, f'SELECT substring({expression} from %s for %s) AS value FROM {table} WHERE {predicate}',
                                    (position, TEXT_CHARS, *params))
            if row is None or not row['value']:
                raise RuntimeError('report text projection changed within snapshot')
            value = row['value']
            raw = value.encode('utf8')
            if len(raw) > FRAME_BYTES or sent + len(raw) > size:
                raise RuntimeError('report text projection exceeds declared size')
            await self.session.send_text(raw)
            sent += len(raw)
            position += len(value)
        self.text_offset += size
        return ref

    async def literal_text(self, key, value):
        # Operator configuration is already resident, but never creates a giant IPC frame.
        size = sum(len(value[i:i + TEXT_CHARS].encode()) for i in range(0, len(value), TEXT_CHARS))
        await self.session.send_command({'type': 'text', 'key': key, 'size': size})
        for i in range(0, len(value), TEXT_CHARS):
            await self.session.send_text(value[i:i + TEXT_CHARS].encode())
        self.text_offset += size

    async def timezone_paths(self):
        # Transfer the application's static operator search paths, not its
        # environment. Existing path strings are escaped in bounded slices.
        paths = zoneinfo.TZPATH
        size = sum(map(len, _timezone_path_chunks(paths)))
        await self.session.send_command({'type':'text','key':'timezone_paths','size':size})
        for chunk in _timezone_path_chunks(paths):
            await self.session.send_text(chunk)
        self.text_offset += size

    async def command_row(self, kind, row):
        await self.session.send_command({'type': kind, 'row': _encode(row)})

    async def rates(self, conn, owner, year):
        row = await self.scalar(conn,
            "SELECT year, octet_length(convert_to(rate_per_mi::text,'UTF8')) AS size1, "
            "octet_length(convert_to(rate_h2_per_mi::text,'UTF8')) AS size2, h2_start_month "
            'FROM mileage_rates WHERE account_id=%s AND year<=%s ORDER BY year DESC LIMIT 1', (owner, year))
        if row is not None:
            await self.text(conn,'rate1',row['size1'],'rate_per_mi::text','mileage_rates','account_id=%s AND year=%s',(owner,row['year']))
            if row['size2'] is not None:
                await self.text(conn,'rate2',row['size2'],'rate_h2_per_mi::text','mileage_rates','account_id=%s AND year=%s',(owner,row['year']))
            row = {'year': row['year'], 'has_rate2': row['size2'] is not None, 'h2_start_month': row['h2_start_month']}
        await self.session.send_command({'type':'rate','row':row})

    async def vehicle_name(self, conn, owner, vehicle):
        if vehicle is None:
            await self.literal_text('vehicle_name','')
            return
        row = await self.scalar(conn,"SELECT octet_length(convert_to(name,'UTF8')) AS size FROM vehicles WHERE account_id=%s AND id=%s",(owner,vehicle))
        if row is None:
            raise RuntimeError('report vehicle is missing')
        await self.text(conn,'vehicle_name',row['size'],'name','vehicles','account_id=%s AND id=%s',(owner,vehicle))

    async def groups(self, conn, owner, bounds, year, annual):
        query = f"""SELECT vehicle_id, 0 AS event, id, started_at,
            category::text AS category, exclusion::text AS exclusion,
            {DISPLAY_DISTANCE_SQL} AS display_distance_m,
            length(btrim(COALESCE(purpose,''), %s))>0 AS purpose_nonblank,
            has_gap, snap_status::text AS snap_status, source::text AS source,
            NULL::date AS incurred_on, NULL::bigint AS amount_size, NULL::text AS treatment,
            NULL::timestamptz AS recorded_at, NULL::double precision AS odometer_m
            FROM trips WHERE account_id=%s AND started_at >= %s AND started_at < %s"""
        params = [_WHITESPACE,owner,bounds['start'],bounds['end']]
        if annual:
            query += """ UNION ALL SELECT vehicle_id,1,id,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                incurred_on,octet_length(convert_to(amount::text,'UTF8')),treatment::text,NULL,NULL FROM expenses
                WHERE account_id=%s AND incurred_on >= %s AND incurred_on < %s
                UNION ALL SELECT vehicle_id,2,id,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                recorded_at,odometer_m FROM odometer_readings WHERE account_id=%s"""
            params.extend((owner,date(year,1,1),date(year+1,1,1),owner))
        query = 'SELECT * FROM (' + query + ') AS events ORDER BY vehicle_id NULLS FIRST,event,started_at,incurred_on,recorded_at,id'
        current = object()
        started = False
        async for row in self.rows(conn,query,params):
            if current != row['vehicle_id']:
                if started:
                    await self.session.send_command({'type':'vehicle_end'})
                current, started = row['vehicle_id'], True
                await self.vehicle_name(conn,owner,current)
                await self.session.send_command({'type':'vehicle_start','id':current})
            event = row.pop('event')
            if event == 1:
                row['amount'] = {'text':await self.text(conn,'expense_amount',row['amount_size'],'amount::text','expenses','account_id=%s AND id=%s',(owner,row['id']))}
            fields = ('id','started_at','category','exclusion','display_distance_m','purpose_nonblank','has_gap','snap_status','source') if event == 0 else ('id','incurred_on','amount','treatment') if event == 1 else ('recorded_at','odometer_m')
            await self.command_row(('vehicle_trip','vehicle_expense','reading')[event],{k:row[k] for k in fields})
            del row
        if started:
            await self.session.send_command({'type':'vehicle_end'})

    async def details(self, conn, owner, bounds):
        descriptors = ','.join(f"octet_length(convert_to(substring({expression} from 1 for 32767),'UTF8')) AS {field}_size" for field,expression in _DETAIL_TEXT.items())
        query = f"""SELECT id,started_at,ended_at,{DISPLAY_DISTANCE_SQL} AS display_distance_m,
            category::text AS category,exclusion::text AS exclusion,has_gap,source::text AS source,
            ST_Y(start_geom::geometry) AS start_lat,ST_X(start_geom::geometry) AS start_lon,
            ST_Y(end_geom::geometry) AS end_lat,ST_X(end_geom::geometry) AS end_lon,{descriptors}
            FROM trips WHERE account_id=%s AND started_at >= %s AND started_at < %s ORDER BY started_at,id"""
        self.portal_number += 1
        async with conn.cursor(name=f'report_{self.portal_number}', row_factory=dict_row) as headers:
            await self.timeout(conn)
            await headers.execute(query,(owner,bounds['start'],bounds['end']))
            while True:
                await self.timeout(conn)
                batch = await headers.fetchmany(FETCH_ROWS)
                if not batch:
                    return
                await self.detail_text_batch(conn,owner,batch)
                for row in batch:
                    await self.command_row('detail',row)
                del row,batch

    async def detail_text_batch(self, conn, owner, batch):
        fields = tuple(_DETAIL_TEXT)
        by_id = {row['id']:row for row in batch}
        sizes = {}
        for row in batch:
            for field in fields:
                size = row.pop(field + '_size')
                sizes[row['id'],field] = size
                row[field] = None if size is None else {'text':[self.text_offset,0]}
        del row
        # Single-row libpq mode avoids one SQL roundtrip per field/chunk. The
        # only variable-width result column is at most TEXT_CHARS characters.
        values = ','.join(f'({i},substring({expression} from 1 for 32767))' for i,expression in enumerate(_DETAIL_TEXT.values()))
        query = f"""SELECT trips.id,field.number,parts.part,
            substring(field.value from 1+(parts.part-1)*%s for %s) AS value
            FROM trips CROSS JOIN LATERAL (VALUES {values}) AS field(number,value)
            CROSS JOIN LATERAL generate_series(1,(char_length(field.value)+%s-1)/%s) AS parts(part)
            WHERE account_id=%s AND id=ANY(%s)
            ORDER BY started_at,trips.id,field.number,parts.part"""
        await self.timeout(conn)
        async with conn.cursor(row_factory=dict_row) as cursor:
            active, left = None, 0
            async with aclosing(cursor.stream(query,(TEXT_CHARS,TEXT_CHARS,TEXT_CHARS,TEXT_CHARS,owner,list(by_id)),size=1)) as stream:
                async for fragment in stream:
                    self.operation.check()
                    field = fields[fragment['number']]
                    key = fragment['id'],field
                    if key != active:
                        if left or fragment['part'] != 1:
                            raise RuntimeError('incomplete report text stream')
                        active, left = key,sizes[key]
                        by_id[key[0]][field] = {'text':[self.text_offset,left]}
                        self.text_offset += left
                        await self.session.send_command({'type':'text','key':'field:'+field,'size':left})
                    raw = fragment['value'].encode('utf8')
                    if not raw or len(raw)>FRAME_BYTES or len(raw)>left:
                        raise RuntimeError('invalid report text stream')
                    await self.session.send_text(raw)
                    left -= len(raw)
                    del fragment,raw
            if left:
                raise RuntimeError('incomplete report text stream')
        for row in batch:
            for field in fields:
                if sizes[row['id'],field] and row[field]['text'][1] != sizes[row['id'],field]:
                    raise RuntimeError('missing report text stream')

    async def expenses(self, conn, owner, year, xlsx):
        extra = ",octet_length(convert_to(substring(notes from 1 for 32767),'UTF8')) AS notes_size" if xlsx else ''
        query = f"""SELECT id,vehicle_id,incurred_on,category::text AS category,octet_length(convert_to(amount::text,'UTF8')) AS amount_size,treatment::text AS treatment{extra}
            FROM expenses WHERE account_id=%s AND incurred_on >= %s AND incurred_on < %s ORDER BY incurred_on,id"""
        async for row in self.rows(conn,query,(owner,date(year,1,1),date(year+1,1,1))):
            row['amount'] = {'text':await self.text(conn,'expense_amount',row.pop('amount_size'),'amount::text','expenses','account_id=%s AND id=%s',(owner,row['id']))}
            if xlsx:
                size = await self.scalar(conn,"SELECT octet_length(convert_to(substring(name from 1 for 32767),'UTF8')) AS size FROM vehicles WHERE account_id=%s AND id=%s",(owner,row['vehicle_id']))
                ref = await self.text(conn,'field:vehicle_name',size['size'],'substring(name from 1 for 32767)','vehicles','account_id=%s AND id=%s',(owner,row['vehicle_id']))
                row['vehicle_name'] = {'text':ref}
                size = row.pop('notes_size')
                row['notes'] = None if size is None else {'text':await self.text(conn,'field:notes',size,'substring(notes from 1 for 32767)','expenses','account_id=%s AND id=%s',(owner,row['id']))}
            await self.command_row('expense',row)
            del row


async def prepare_report(request, user, kind, year=None, start=None, end=None):
    """Prepare a complete file under the already admitted actual operation owner."""
    if kind not in ('annual_html','annual_xlsx','range_html','range_xlsx'):
        raise ValueError('unknown report kind')
    annual, xlsx = kind.startswith('annual'), kind.endswith('xlsx')
    if annual:
        start, end = date(year,1,1), date(year,12,31)
    else:
        year = start.year
    operation = request.state._preparation
    session = await operation.start_helper(mode='report')
    projection = Projection(operation,session)
    control = request.state._report_control_connection
    principal = request.state.principal
    async with control.transaction():
        await control.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        await projection.timeout(control)
        row = await projection.scalar(control,
            "SELECT octet_length(convert_to(email,'UTF8')) AS size FROM accounts WHERE id=%s AND is_enabled AND auth_version=%s",
            (principal.account_id,principal.auth_version))
        if row is None:
            raise RuntimeError('report account binding changed')
        snapshot = await projection.scalar(control,'SELECT pg_export_snapshot() AS snapshot')
        await projection.text(control,'email',row['size'],'email','accounts','id=%s',(principal.account_id,))
        async with request.state.account_pool.connection(snapshot_id=snapshot['snapshot']) as conn:
            owner = account_id(conn)
            settings = await projection.scalar(conn,"SELECT octet_length(convert_to(display_tz,'UTF8')) AS size FROM account_settings WHERE account_id=%s",(owner,))
            if settings is None:
                raise RuntimeError('account preferences are missing')
            await projection.text(conn,'display_tz',settings['size'],'display_tz','account_settings','account_id=%s',(owner,))
            await projection.timezone_paths()
            await projection.literal_text('app_version',request.app.state.config.app_version)
            await projection.literal_text('csrf',request.session.get('csrf',''))
            await projection.literal_text('csp_nonce',getattr(request.state,'csp_nonce',''))
            await projection.timeout(conn)
            review_count = await _fetch_review_count(conn)
            await projection.timeout(conn)
            storage = await storage_status(conn)
            bounds = await session.request({'type':'initialize','metadata':dict(kind=kind,start=start.isoformat(),end=end.isoformat(),user=user,
                review_count=review_count,storage=storage,request_path=request.url.path)})
            bounds = {k:datetime.fromisoformat(v) for k,v in bounds.items()}
            await projection.rates(conn,owner,year)
            query = f'SELECT {_TRIP_SCALARS} FROM trips WHERE account_id=%s AND started_at >= %s AND started_at < %s ORDER BY started_at,id'
            async for row in projection.rows(conn,query,(_WHITESPACE,owner,bounds['start'],bounds['end'])):
                await projection.command_row('global_trip',row)
                del row
            await projection.groups(conn,owner,bounds,year,annual)
            if xlsx:
                await projection.details(conn,owner,bounds)
            if annual:
                await projection.expenses(conn,owner,year,xlsx)
            result = await session.request({'type':'render'})
            await session.finish_input()
    await operation.finish_preparation()
    return operation.prepared(result['path'],media_type=result['media_type'],filename=result['filename'])
