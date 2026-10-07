"""Complete isolated NTFY rendering and inherited cookie record bounds."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.notification_preparation import Projection
from app.ntfy_cookies import COOKIE_RECORD_BYTES, cookie_chunks, restore_cookie
from app.ntfy_preparation import NtfyJob, _cookie_state
from app.nudge import latest_window_end, nudge_message
from app.odometer import latest_quarter_start
from app.odometer_reminder import reminder_message
from app.preparation import PreparationOperation
from app.prepared_ntfy import PreparedNtfy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind,count', [('weekly', 0), ('weekly', 1), ('weekly', 71),
                                        ('quarterly', 0), ('quarterly', 1), ('quarterly', 3)])
def test_real_helper_complete_body_and_no_send_skip_match_pure_oracles(tmp_path, kind, count):
    async def scenario():
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        async with manager.operation('background', principal):
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                session = await operation.start_helper('ntfy')
                projection = Projection(operation, session)
                app_url = 'https://example.test/' + '界' * 40000 + '///'
                for key, value in (('display_tz', 'America/New_York'), ('ntfy_topic', 'topic'),
                                   ('app_url', app_url), ('url', 'http://example.test'),
                                   ('token', ''), ('username', ''), ('password', 'large-' + '界' * 50000)):
                    await projection.literal(key, value)
                await projection.timezone_paths()
                now = datetime.fromisoformat('2026-11-01T20:00:00-05:00')
                result = await session.request({'type': 'initialize', 'kind': kind, 'hour': 18,
                                                'now': now.isoformat()})
                end = datetime.fromisoformat(result['end'])
                from zoneinfo import ZoneInfo
                local_now = now.astimezone(ZoneInfo('America/New_York'))
                expected_end = latest_window_end(local_now, 18) if kind == 'weekly' else latest_quarter_start(local_now, 18)
                assert end == expected_end
                names = ['é, vehicle-' + '界' * 50000, '', 'third\nfull name'][:count]
                if kind == 'quarterly':
                    for index, name in enumerate(names):
                        await projection.literal('vehicle_name', name)
                        await session.send_command({'type': 'vehicle', 'id': index, 'due': True})
                    # Already logged active names still fully decode, but never
                    # enter the due-body list.
                    await session.send_command({'type': 'text', 'key': 'vehicle_name', 'size': 1,
                                                'encoding': 'utf8', 'keep': False})
                    await session.send_text(b'x')
                    await session.send_command({'type': 'vehicle', 'id': 99, 'due': False})
                async with httpx.AsyncClient() as held:
                    held.cookies.set('initial', 'x', domain='example.test')
                    cookie = next(iter(held.cookies.jar))
                    cookie.comment = 'arbitrary held comment ' + '界' * 50000
                    job = NtfyJob(operation, session, kind, end, end, 18, True)
                    if count:
                        await _cookie_state(job, held)
                    prepared_result = await session.request({'type': 'render', 'count': count})
                    await session.finish_input()
                    if not count:
                        assert prepared_result == {'count': 0, 'artifacts': None}
                    else:
                        prepared = PreparedNtfy(operation.reservation, prepared_result['artifacts'])
                        stack, descriptors, guard = prepared.open_descriptors()
                        try:
                            await operation.finish_preparation()
                            import os
                            with os.fdopen(os.dup(descriptors[1]), 'rb') as source:
                                actual = source.read().decode('utf8')
                            expected = nudge_message(count, end, app_url) if kind == 'weekly' else reminder_message(sorted(names), app_url)
                            assert actual == expected
                            with os.fdopen(os.dup(descriptors[0]), 'rb') as source:
                                config = json.load(source)
                            assert config['password'].endswith('界' * 50000)
                            assert config['body_bytes'] == len(expected.encode('utf8'))
                            with os.fdopen(os.dup(descriptors[2]), 'rb') as source:
                                restored = restore_cookie(json.loads(source.readline()))
                            assert restored.comment == cookie.comment
                        finally:
                            stack.close()
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


def test_locked_sources_imply_cookie_record_bound_without_new_cookie_cap():
    from httpcore._async.http11 import AsyncHTTP11Connection
    from httpx._urlparse import MAX_URL_LENGTH
    from http.cookiejar import request_path
    assert AsyncHTTP11Connection.MAX_INCOMPLETE_EVENT_SIZE == 100 * 1024
    assert AsyncHTTP11Connection.READ_NUM_BYTES == 64 * 1024
    assert MAX_URL_LENGTH == 65536
    url = httpx.URL('http://example.test/' + '\U0001f680' * (65536 - len('http://example.test/')))
    assert len(str(url)) <= 12 * MAX_URL_LENGTH
    request = httpx.Cookies._CookieCompatRequest(httpx.Request('GET', url))
    assert len(request_path(request)) <= 12 * MAX_URL_LENGTH
    response = httpx.Response(200, request=request.request,
        headers={'Set-Cookie': 'large=' + 'x' * 90000 + '; custom=' + 'y' * 9000})
    cookies = httpx.Cookies()
    cookies.extract_cookies(response)
    cookie = next(iter(cookies.jar))
    encoded = b''.join(cookie_chunks(cookie))
    assert 65520 < len(encoded) < COOKIE_RECORD_BYTES
    assert restore_cookie(json.loads(encoded))._rest == cookie._rest


@pytest.mark.parametrize('count', [0, 1])
def test_operator_surrogate_is_preserved_until_actual_body_use(tmp_path, count):
    async def scenario():
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        async with manager.operation('background', principal):
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                session = await operation.start_helper('ntfy')
                projection = Projection(operation, session)
                for key, value in (('display_tz', 'UTC'), ('ntfy_topic', 'topic'),
                    ('app_url', 'https://example.test/\ud800'), ('url', 'http://example.test'),
                    ('token', ''), ('username', ''), ('password', '\ud800unused')):
                    await projection.literal(key, value)
                await projection.timezone_paths()
                await session.request({'type': 'initialize', 'kind': 'weekly', 'hour': 18,
                                       'now': '2026-11-02T19:00:00+00:00'})
                if count:
                    await projection.literal('environment', '{}')
                    with pytest.raises(UnicodeEncodeError):
                        await session.request({'type': 'render', 'count': count})
                else:
                    assert await session.request({'type': 'render', 'count': count}) == {'count': 0, 'artifacts': None}
                    await session.finish_input()
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


def test_already_logged_active_name_still_requires_complete_decode(tmp_path):
    async def scenario():
        manager = AdmissionManager()
        principal = AccountPrincipal(41, True, 1)
        async with manager.operation('background', principal):
            async with PreparationOperation(spool_root=tmp_path / 'spool') as operation:
                session = await operation.start_helper('ntfy')
                await session.send_command({'type': 'text', 'key': 'vehicle_name', 'size': 2,
                                            'encoding': 'utf8', 'keep': False})
                await session.send_text(b'x\xff')
                with pytest.raises(UnicodeDecodeError):
                    await session.request({'type': 'count'})
        assert not list((tmp_path / 'spool').glob('op-*'))
    asyncio.run(scenario())


@pytest.mark.parametrize('value', ['\ud800\udc80', '\U00010080', 'x' * 4095 + '\ud800\udc80'])
def test_initial_cookie_fields_rest_keys_and_deletion_preserve_python_strings(value):
    from app.ntfy_cookies import apply_cookie
    original = httpx.Cookies()
    original.set('name-' + value, value, domain='example.test', path='/' + value)
    cookie = next(iter(original.jar))
    cookie.comment = cookie.comment_url = value
    cookie._rest = {'key-' + value: value}
    raw = b''.join(cookie_chunks(cookie))
    restored = restore_cookie(json.loads(raw))
    for field in ('name', 'value', 'path', 'comment', 'comment_url', '_rest'):
        assert getattr(restored, field) == getattr(cookie, field)
    held = httpx.Cookies()
    apply_cookie(held.jar, b'S', raw)
    assert b''.join(cookie_chunks(next(iter(held.jar)))) == raw
    deletion = json.dumps([cookie.domain, cookie.path, cookie.name], ensure_ascii=False).encode('utf8', 'surrogatepass')
    apply_cookie(held.jar, b'X', deletion)
    assert not list(held.jar)
