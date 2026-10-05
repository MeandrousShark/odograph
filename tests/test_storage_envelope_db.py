"""Device-wide prepaid output checks against real detector reconciliation."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import os

import psycopg
import pytest

from app.account_context import account_id
from app.db import make_pool
from app.detector.core import Params, Point
from app.detector.runner import DetectorRunner
from conftest import reset_account_db, seed_tracking_device
from tests.synth import Drive, Stationary, T0, build_track

TEST_DB = os.environ.get('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not TEST_DB, reason='requires disposable TEST_DATABASE_URL')
LONG_LABEL = '\U0001f680' * 100


async def _insert_points(conn, device, points, label):
    ids = []
    for p in points:
        row = await (await conn.execute(
            'INSERT INTO points(account_id,tracking_device_id,device,recorded_at,received_at,geom,accuracy_m) '
            'VALUES(%s,%s,%s,%s,now(),ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s) RETURNING id',
            (account_id(conn), device, label, p.t, p.lon, p.lat, p.accuracy_m),
        )).fetchone()
        ids.append(row[0])
    return ids


async def _force(conn, device, ids, label):
    for point in ids:
        await conn.execute(
            'INSERT INTO trip_boundary_overrides(account_id,tracking_device_id,device,kind,point_id) '
            "VALUES(%s,%s,%s,'force',%s)", (account_id(conn), device, label, point),
        )


async def _assert_envelope(conn, device, label_bytes):
    owner = account_id(conn)
    actual = await (await conn.execute(
        'SELECT (SELECT count(*) FROM points WHERE account_id=%s AND tracking_device_id=%s), '
        '(SELECT count(*) FROM stays WHERE account_id=%s AND tracking_device_id=%s), '
        "(SELECT count(*) FROM trips WHERE account_id=%s AND tracking_device_id=%s AND source='detected' AND NOT imported), "
        "(SELECT coalesce(sum(ST_NPoints(path)),0) FROM trips WHERE account_id=%s AND tracking_device_id=%s AND source='detected' AND NOT imported), "
        '(SELECT coalesce(sum(256+octet_length(device)+octet_length(ST_AsEWKB(centroid::geometry,\'NDR\'))),0) '
        ' FROM stays WHERE account_id=%s AND tracking_device_id=%s) + '
        '(SELECT coalesce(sum(512+octet_length(device)+coalesce(octet_length(ST_AsEWKB(start_geom::geometry,\'NDR\')),0) '
        '+coalesce(octet_length(ST_AsEWKB(end_geom::geometry,\'NDR\')),0)+coalesce(octet_length(ST_AsEWKB(path,\'NDR\')),0)),0) '
        " FROM trips WHERE account_id=%s AND tracking_device_id=%s AND source='detected' AND NOT imported)",
        (owner, device) * 6,
    )).fetchone()
    n, stays, trips, vertices, core = actual
    assert stays <= n
    assert trips <= max(n - 1, 0)
    assert vertices <= 2 * n
    assert core <= n * (1024 + 2 * label_bytes)
    protected = await (await conn.execute(
        'SELECT point_count,stay_count,trip_count,path_vertices,core_bytes,label_bytes '
        'FROM device_storage_envelopes WHERE account_id=%s AND tracking_device_id=%s',
        (owner, device),
    )).fetchone()
    assert tuple(protected) == tuple(actual) + (label_bytes,)
    usage = await (await conn.execute(
        'SELECT reserved_bytes FROM account_usage WHERE account_id=%s', (owner,),
    )).fetchone()
    assert usage[0] == n * (1024 + 2 * label_bytes)
    return tuple(actual)


async def _fixture(raw_pool, label=LONG_LABEL):
    pool = await reset_account_db(raw_pool)
    async with pool.connection() as conn:
        device = await seed_tracking_device(conn, label)
    return pool, device


@pytest.mark.parametrize('n', [1, 2, 37])
def test_forced_boundaries_and_label_rename_keep_permanent_highwater(n):
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            initial_label = '\u8eca' * 100
            pool, device = await _fixture(raw, initial_label)
            points = [Point(T0 + timedelta(seconds=15 * i), 45.5, -122.6 + .004 * i)
                      for i in range(n)]
            async with pool.connection() as conn:
                ids = await _insert_points(conn, device, points, initial_label)
                await _force(conn, device, ids, initial_label)
            runner = DetectorRunner(pool, replace(Params(), min_trip_distance_m=0))
            await runner.reprocess_device_now(device)
            async with pool.connection() as conn:
                before = await _assert_envelope(conn, device, 300)
                assert before[:3] == (n, n, max(n - 1, 0))
                assert before[3] == 2 * max(n - 1, 0)
                await conn.execute('UPDATE tracking_devices SET label=%s WHERE id=%s', (LONG_LABEL, device))
            # A new runner and full reconstruction cannot spend or shrink the reserve.
            await DetectorRunner(pool, runner.params).reprocess_device_now(device)
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                await conn.execute('UPDATE tracking_devices SET label=%s WHERE id=%s', ('p', device))
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_retained_prefix_dirty_suffix_restart_and_full_reprocess_preserve_personal_rows():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool, device = await _fixture(raw)
            first = build_track([Stationary(600), Drive(2), Stationary(600),
                                 Drive(2), Stationary(600)])
            async with pool.connection() as conn:
                await _insert_points(conn, device, first, LONG_LABEL)
            runner = DetectorRunner(pool, Params())
            assert await runner.run_once()
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                row = await (await conn.execute(
                    "SELECT id,started_at,ended_at,ST_AsEWKB(path) FROM trips WHERE source='detected' ORDER BY started_at LIMIT 1",
                )).fetchone()
                first_trip = tuple(row)
                usage_before = (await (await conn.execute(
                    'SELECT actual_bytes FROM account_usage WHERE account_id=%s', (account_id(conn),),
                )).fetchone())[0]
                human_notes = LONG_LABEL * 10
                await conn.execute(
                    "UPDATE trips SET purpose='client visit',notes=%s,category='business',tag_source='human' WHERE id=%s",
                    (human_notes, first_trip[0]),
                )
                usage_after = (await (await conn.execute(
                    'SELECT actual_bytes FROM account_usage WHERE account_id=%s', (account_id(conn),),
                )).fetchone())[0]
                assert usage_after - usage_before == len('client visit') + len(human_notes.encode('utf-8'))
                for source, imported in [('manual', False), ('detected', True)]:
                    await conn.execute(
                        'INSERT INTO trips(account_id,tracking_device_id,device,source,imported,started_at,ended_at,distance_m,purpose,notes) '
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,1000,'preserved purpose','preserved notes')",
                        (account_id(conn), device, LONG_LABEL * 3, source, imported, first[0].t, first[-1].t),
                    )
                personal_before = await (await conn.execute(
                    "SELECT id,source::text,imported,device,purpose,notes FROM trips WHERE source='manual' OR imported ORDER BY id",
                )).fetchall()
                # Move overlap receipt timestamps behind the checkpoint so only the new suffix is dirty.
                await conn.execute("UPDATE points SET received_at='2020-01-01' WHERE tracking_device_id=%s", (device,))
            suffix = build_track([Drive(3), Stationary(600)], start=(first[-1].lat, first[-1].lon),
                                 t0=first[-1].t)[1:]
            async with pool.connection() as conn:
                await _insert_points(conn, device, suffix, LONG_LABEL)
            assert await DetectorRunner(pool, Params()).run_once()
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                preserved = await (await conn.execute(
                    'SELECT id,started_at,ended_at,ST_AsEWKB(path) FROM trips WHERE id=%s', (first_trip[0],),
                )).fetchone()
                assert tuple(preserved) == first_trip
            await DetectorRunner(pool, Params()).reprocess_device_now(device)
            async with pool.connection() as conn:
                await _assert_envelope(conn, device, 400)
                personal_after = await (await conn.execute(
                    "SELECT id,source::text,imported,device,purpose,notes FROM trips WHERE source='manual' OR imported ORDER BY id",
                )).fetchall()
                assert personal_after == personal_before
                human = await (await conn.execute(
                    'SELECT purpose,notes,category::text,tag_source::text FROM trips WHERE id=%s', (first_trip[0],),
                )).fetchone()
                assert tuple(human) == ('client visit', human_notes, 'business', 'human')
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_point_refund_requires_final_output_reconciliation_and_allows_intermediate_excess():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool, device = await _fixture(raw, 'phone')
            points = [Point(T0 + timedelta(seconds=15 * i), 45.5, -122.6 + .004 * i) for i in range(8)]
            async with pool.connection() as conn:
                ids = await _insert_points(conn, device, points, 'phone')
                await _force(conn, device, ids, 'phone')
            await DetectorRunner(pool, replace(Params(), min_trip_distance_m=0)).reprocess_device_now(device)
            async with pool.connection() as conn:
                before = await _assert_envelope(conn, device, 5)
            with pytest.raises(psycopg.errors.CheckViolation, match='retained point envelope'):
                async with pool.connection() as conn:
                    await conn.execute('DELETE FROM trip_boundary_overrides WHERE tracking_device_id=%s', (device,))
                    await conn.execute('DELETE FROM points WHERE tracking_device_id=%s', (device,))
            async with pool.connection() as conn:
                assert await _assert_envelope(conn, device, 5) == before
            async with pool.connection() as conn:
                await conn.execute('DELETE FROM trip_boundary_overrides WHERE tracking_device_id=%s', (device,))
                await conn.execute('DELETE FROM points WHERE tracking_device_id=%s', (device,))
                # The same final state succeeds even with points deleted before output.
                await conn.execute('DELETE FROM stays WHERE tracking_device_id=%s', (device,))
                await conn.execute("DELETE FROM trips WHERE tracking_device_id=%s AND source='detected' AND NOT imported", (device,))
            async with pool.connection() as conn:
                assert await _assert_envelope(conn, device, 5) == (0, 0, 0, 0, 0)
        finally:
            await raw.close()
    asyncio.run(scenario())


def test_excess_raw_vertices_roll_back_but_temporary_stay_replacement_is_allowed():
    async def scenario():
        raw = make_pool(TEST_DB)
        await raw.open(wait=True)
        try:
            pool, device = await _fixture(raw, 'phone')
            points = [Point(T0 + timedelta(seconds=15 * i), 45.5, -122.6 + .004 * i) for i in range(3)]
            async with pool.connection() as conn:
                ids = await _insert_points(conn, device, points, 'phone')
                await _force(conn, device, ids, 'phone')
            await DetectorRunner(pool, replace(Params(), min_trip_distance_m=0)).reprocess_device_now(device)
            async with pool.connection() as conn:
                before = await _assert_envelope(conn, device, 5)
            with pytest.raises(psycopg.errors.CheckViolation, match='retained point envelope'):
                async with pool.connection() as conn:
                    await conn.execute(
                        "UPDATE trips SET path=ST_GeomFromText('LINESTRING(0 0,1 0,2 0,3 0,4 0,5 0,6 0)',4326) "
                        'WHERE id=(SELECT min(id) FROM trips WHERE tracking_device_id=%s)', (device,),
                    )
            async with pool.connection() as conn:
                assert await _assert_envelope(conn, device, 5) == before
                inserted = await (await conn.execute(
                    'INSERT INTO stays(account_id,tracking_device_id,device,started_at,ended_at,centroid,point_count) '
                    'SELECT account_id,tracking_device_id,device,started_at,ended_at,centroid,point_count '
                    'FROM stays WHERE tracking_device_id=%s ORDER BY id LIMIT 1 RETURNING id', (device,),
                )).fetchone()
                await conn.execute('DELETE FROM stays WHERE id=%s', (inserted[0],))
            async with pool.connection() as conn:
                assert await _assert_envelope(conn, device, 5) == before
        finally:
            await raw.close()
    asyncio.run(scenario())
