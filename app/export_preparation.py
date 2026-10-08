"""Bounded generic export projection inside the admitted lifecycle snapshot."""
from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from datetime import datetime

from fastapi import HTTPException
from psycopg.rows import dict_row

from app.account_context import account_id
from app.capacity import owned_thread
from app.export_filters import RECORD, SEARCH_SETTING
from app.preparation import FRAME_BYTES, _settle
from app.report_preparation import Projection, FETCH_ROWS, TEXT_CHARS, _DETAIL_TEXT
from app.trip_queries import DISPLAY_DISTANCE_SQL
from app.ui._common import _trip_filter_sql, _START_PLACE_NAME_SQL, _END_PLACE_NAME_SQL, _START_ADDRESS_SQL, _END_ADDRESS_SQL


@asynccontextmanager
async def pattern_source(budget):
    opening = asyncio.create_task(owned_thread(budget.open, 'export-pattern', 'rb'))
    source = None
    try:
        try:
            source = await asyncio.shield(opening)
        except asyncio.CancelledError:
            source, _ = await _settle(opening)
            raise
        yield source
    finally:
        if source is not None:
            closing = asyncio.create_task(owned_thread(source.close))
            _, cancelled = await _settle(closing)
            if cancelled:
                raise asyncio.CancelledError


async def assemble_search_pattern(projection,conn,metadata):
    if not metadata['search']:
        return
    if metadata['pattern_path']!='export-pattern':
        raise ValueError('invalid export pattern artifact')
    await projection.scalar(conn,f"SELECT octet_length(set_config('{SEARCH_SETTING}','',true)) AS size")
    total = count = 0
    async with pattern_source(projection.operation.budget) as source:
        while header := await owned_thread(source.read, RECORD.size):
            if len(header)!=RECORD.size:
                raise ValueError('incomplete export search record')
            size, = RECORD.unpack(header)
            if not 0<size<=FRAME_BYTES:
                raise ValueError('invalid export search record')
            value = await owned_thread(source.read, size)
            if len(value)!=size:
                raise ValueError('incomplete export search record')
            await projection.scalar(conn,f"SELECT octet_length(set_config('{SEARCH_SETTING}',"
                f"current_setting('{SEARCH_SETTING}') || convert_from(%b,%s),true)) AS size",
                (value,conn.info.parameter_status('client_encoding')))
            total += size
            count += 1
            del value
    if total!=metadata['pattern_bytes'] or count!=metadata['pattern_records']:
        raise ValueError('incomplete export search artifact')
    await owned_thread(projection.operation.budget.remove, 'export-pattern')


def filter_sql(metadata,owner):
    where,params = _trip_filter_sql(metadata['category'],
        datetime.fromisoformat(metadata['from_dt']) if metadata['from_dt'] else None,
        datetime.fromisoformat(metadata['to_dt']) if metadata['to_dt'] else None,
        metadata['vehicle'],exclusion=metadata['exclusion'],owner_id=owner)
    if metadata['search']:
        expressions = ('notes','purpose',_START_PLACE_NAME_SQL,_END_PLACE_NAME_SQL,'start_label','end_label',_START_ADDRESS_SQL,_END_ADDRESS_SQL)
        where += ' AND (' + ' OR '.join(f"{value} ILIKE current_setting('{SEARCH_SETTING}') ESCAPE '\\'" for value in expressions) + ')'
    return where,params


class ExportProjection(Projection):
    async def literal_text(self, key, value):
        # Request parsing already owns the input; admission bounds every new copy
        # and keeps even its descriptor pass responsive to cancellation.
        size = 0
        for offset in range(0, len(value), TEXT_CHARS):
            self.operation.check()
            size += len(value[offset:offset + TEXT_CHARS].encode('utf8'))
            await asyncio.sleep(0)
        await self.session.send_command({'type': 'text', 'key': key, 'size': size})
        for offset in range(0, len(value), TEXT_CHARS):
            self.operation.check()
            await self.session.send_text(value[offset:offset + TEXT_CHARS].encode('utf8'))
        self.text_offset += size

    async def text(self, conn, key, size, expression, table, predicate, params):
        size = size or 0
        codec = conn.info.encoding
        ref = [self.text_offset, size, codec]
        await self.session.send_command({'type': 'text', 'key': key, 'size': size, 'encoding': codec})
        for offset in range(0, size, FRAME_BYTES):
            row = await self.scalar(conn,
                f'SELECT substring(convert_to({expression},%s) from %s for %s) AS value FROM {table} WHERE {predicate}',
                (conn.info.parameter_status('client_encoding'), offset + 1, FRAME_BYTES, *params))
            value = bytes(row['value']) if row is not None else b''
            if len(value) != min(FRAME_BYTES, size - offset):
                raise RuntimeError('incomplete export source stream')
            await self.session.send_text(value)
        self.text_offset += size
        return ref

    async def rates(self,conn,owner):
        # Datetime years are 1..9999. Only one earlier predecessor can affect
        # that finite domain, even if stored integer years extend beyond it.
        query = """SELECT year,octet_length(convert_to(rate_per_mi::text,%s)) AS size1,
            octet_length(convert_to(rate_h2_per_mi::text,%s)) AS size2,h2_start_month
            FROM mileage_rates WHERE account_id=%s AND (year BETWEEN 1 AND 9999 OR
                year=(SELECT max(year) FROM mileage_rates WHERE account_id=%s AND year<=0)) ORDER BY year"""
        async for row in self.rows(conn,query,(conn.info.parameter_status('client_encoding'),conn.info.parameter_status('client_encoding'),owner,owner)):
            await self.text(conn,'rate1',row['size1'],'rate_per_mi::text','mileage_rates','account_id=%s AND year=%s',(owner,row['year']))
            if row['size2'] is not None:
                await self.text(conn,'rate2',row['size2'],'rate_h2_per_mi::text','mileage_rates','account_id=%s AND year=%s',(owner,row['year']))
            await self.session.send_command({'type':'rate','row':dict(year=row['year'],has_rate2=row['size2'] is not None,h2_start_month=row['h2_start_month'])})
            del row

    async def details(self,conn,owner,where,params,xlsx):
        # Full client conversion/decoding precedes the child's XLSX prefix.
        expressions = _DETAIL_TEXT
        descriptors = ','.join(f"octet_length(convert_to({value},%s)) AS {field}_size" for field,value in expressions.items())
        query = f"""SELECT id,started_at,ended_at,{DISPLAY_DISTANCE_SQL} AS display_distance_m,
            category::text AS category,exclusion::text AS exclusion,has_gap,source::text AS source,
            ST_Y(start_geom::geometry) AS start_lat,ST_X(start_geom::geometry) AS start_lon,
            ST_Y(end_geom::geometry) AS end_lat,ST_X(end_geom::geometry) AS end_lon,{descriptors}
            FROM trips {where} ORDER BY started_at DESC"""
        self.portal_number += 1
        async with conn.cursor(name=f'export_{self.portal_number}',row_factory=dict_row) as headers:
            await self.timeout(conn)
            await headers.execute(query,(*([conn.info.parameter_status('client_encoding')]*len(expressions)),*params))
            while True:
                await self.timeout(conn)
                batch = await headers.fetchmany(FETCH_ROWS)
                if not batch:
                    return
                await self.text_batch(conn,owner,batch,expressions)
                for row in batch:
                    await self.command_row('detail',row)
                del row,batch

    async def text_batch(self,conn,owner,batch,expressions):
        fields = tuple(expressions)
        by_id = {row['id']:row for row in batch}
        sizes = {}
        for row in batch:
            for field in fields:
                size = row.pop(field+'_size')
                sizes[row['id'],field] = size
                row[field] = None if size is None else {'text':[self.text_offset,0,conn.info.encoding]}
        del row
        values = ','.join(f'({i},convert_to({value},%s))' for i,value in enumerate(expressions.values()))
        query = f"""SELECT trips.id,field.number,parts.part,
            substring(field.value from 1+(parts.part-1)*%s for %s) AS value
            FROM unnest(%s::bigint[]) WITH ORDINALITY AS chosen(id,ordinal)
            JOIN trips ON trips.id=chosen.id CROSS JOIN LATERAL (VALUES {values}) AS field(number,value)
            CROSS JOIN LATERAL generate_series(1,(octet_length(field.value)+%s-1)/%s) AS parts(part)
            WHERE account_id=%s ORDER BY chosen.ordinal,field.number,parts.part"""
        await self.timeout(conn)
        async with conn.cursor(row_factory=dict_row) as cursor:
            active,left = None,0
            async with aclosing(cursor.stream(query,(FRAME_BYTES,FRAME_BYTES,list(by_id),*([conn.info.parameter_status('client_encoding')]*len(expressions)),FRAME_BYTES,FRAME_BYTES,owner),size=1)) as stream:
                async for fragment in stream:
                    self.operation.check()
                    key = fragment['id'],fields[fragment['number']]
                    if key!=active:
                        if left or fragment['part']!=1:
                            raise RuntimeError('incomplete export source stream')
                        active,left = key,sizes[key]
                        by_id[key[0]][key[1]] = {'text':[self.text_offset,left,conn.info.encoding]}
                        self.text_offset += left
                        await self.session.send_command({'type':'text','key':'field:'+key[1],'size':left,'encoding':conn.info.encoding})
                    raw = bytes(fragment['value'])
                    if not raw or len(raw)>FRAME_BYTES or len(raw)>left:
                        raise RuntimeError('invalid export source stream')
                    await self.session.send_text(raw)
                    left -= len(raw)
                    del fragment,raw
            if left:
                raise RuntimeError('incomplete export source stream')
        for row in batch:
            for field in fields:
                if sizes[row['id'],field] and row[field]['text'][1]!=sizes[row['id'],field]:
                    raise RuntimeError('missing export source stream')


async def prepare_export(request,user,format='csv',category='',from_='',to='',vehicle='',q='',exclusion=''):
    if format not in ('csv','xlsx'):
        raise HTTPException(status_code=400,detail='format must be csv or xlsx')
    operation = request.state._preparation
    session = await operation.start_helper(mode='export')
    projection = ExportProjection(operation,session)
    control = request.state._report_control_connection
    principal = request.state.principal
    async with control.transaction():
        await control.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        guard = await projection.scalar(control,'SELECT id FROM accounts WHERE id=%s AND is_enabled AND auth_version=%s',(principal.account_id,principal.auth_version))
        if guard is None:
            raise RuntimeError('export account binding changed')
        snapshot = await projection.scalar(control,'SELECT pg_export_snapshot() AS snapshot')
        async with request.state.account_pool.connection(snapshot_id=snapshot['snapshot']) as conn:
            owner = account_id(conn)
            settings = await projection.scalar(conn,"SELECT octet_length(convert_to(display_tz,%s)) AS size FROM account_settings WHERE account_id=%s",(conn.info.parameter_status('client_encoding'),owner))
            if settings is None:
                raise RuntimeError('account preferences are missing')
            await projection.text(conn,'display_tz',settings['size'],'display_tz','account_settings','account_id=%s',(owner,))
            await projection.timezone_paths()
            for key,value in (('category',category),('from',from_),('to',to),('vehicle',vehicle),('q',q),('exclusion',exclusion if isinstance(exclusion,str) else '')):
                await projection.literal_text(key,value)
            codec = conn.info.encoding
            metadata = await session.request({'type':'initialize','metadata':{'format':format,'client_codec':'utf8' if codec=='ascii' else codec}})
            await assemble_search_pattern(projection,conn,metadata)
            where,params = filter_sql(metadata,owner)
            await projection.rates(conn,owner)
            await projection.details(conn,owner,where,params,format=='xlsx')
            result = await session.request({'type':'render'})
            await session.finish_input()
    await operation.finish_preparation()
    return operation.prepared(result['path'],media_type=result['media_type'],filename=result['filename'])
