"""Exact helper resource classification; fault injection is not a native quota proof."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import ntfy_supervisor, smtp_supervisor
from test_prepared_ntfy_ops import prepare as prepare_ntfy
from test_prepared_smtp_ops import prepare as prepare_smtp
from test_smtp_supervisor_ops import assert_reaped, observe_spawn

pytestmark = pytest.mark.ops

ERRORS = {
    'memory': 'MemoryError()',
    'enomem': "OSError(errno.ENOMEM, 'fixture allocation failure')",
    'io': "OSError(errno.EACCES, 'fixture permission failure')",
    'lookup': "LookupError('fixture lookup failure')",
    'import': "ImportError('fixture import failure')",
    'ssl': "ssl.SSLError(ssl.SSL_ERROR_SSL, 'fixture TLS failure')",
}


def injected_helper(tmp_path, target, error, *, startup=False, entry=False, phase=None):
    helper = tmp_path / 'injected-helper.py'
    if entry:
        content = '''import errno, sys
def trace(frame, event, arg):
    if event == 'call' and frame.f_code.co_filename == TARGET and frame.f_code.co_name == 'main':
        raise ERROR
    return trace
sys.settrace(trace)
with open(TARGET, 'rb') as source:
    code = compile(source.read(), TARGET, 'exec')
exec(code, {'__name__': '__main__', '__file__': TARGET})
'''
    elif startup:
        # Execute the canonical entry, including AS installation before IDNA.
        content = '''import builtins, errno, resource, sys
original_import = builtins.__import__
def injected_import(name, *args, **kwargs):
    if name == 'encodings.idna':
        if sys.platform == 'linux':
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            assert 0 < soft <= 256 * 1024 * 1024 and soft == hard
        raise ERROR
    return original_import(name, *args, **kwargs)
builtins.__import__ = injected_import
with open(TARGET, 'rb') as source:
    code = compile(source.read(), TARGET, 'exec')
exec(code, {'__name__': '__main__', '__file__': TARGET})
'''
    else:
        asynchronous = target.name == 'ntfy_helper.py'
        function = 'transport' if asynchronous else 'prepared_transport'
        content = '''import errno, runpy, ssl
namespace = runpy.run_path(TARGET)
PREFIXdef injected_transport(*args):
    args[-1]['phase'] = PHASE
    raise ERROR
namespace['main'].__globals__[FUNCTION] = injected_transport
raise SystemExit(namespace['main']())
'''.replace('PREFIX', 'async ' if asynchronous else '')
        content = content.replace('PHASE', repr(phase)).replace('FUNCTION', repr(function))
    helper.write_text(content.replace('TARGET', repr(str(target))).replace('ERROR', ERRORS[error]))
    return helper


async def classified_attempt(monkeypatch, tmp_path, kind, helper, error, phase):
    supervisor = smtp_supervisor if kind == 'smtp' else ntfy_supervisor
    monkeypatch.setattr(supervisor, 'HELPER_PATH' if kind == 'smtp' else 'HELPER', helper)
    processes, _ = observe_spawn(monkeypatch)
    async with httpx.AsyncClient() as held:
        prepared = (prepare_smtp(tmp_path / 'spool', 1) if kind == 'smtp'
                    else prepare_ntfy(tmp_path / 'spool', 'http://127.0.0.1:1', held, body='fixture'))
        try:
            exception = supervisor.SMTPTransportError if kind == 'smtp' else supervisor.NtfyTransportError
            with pytest.raises(exception) as failure:
                if kind == 'smtp':
                    await supervisor.send_prepared(prepared)
                else:
                    await supervisor.send_prepared(prepared, http_client=held)
            resource = error in ('memory', 'enomem')
            assert (failure.value.failure == 'resource') == resource
            if resource or kind == 'smtp' or phase == 'readiness':
                assert failure.value.phase == phase
            assert failure.value.cleanup_confirmed
            assert processes[0].returncode == (73 if resource else 1)
            assert_reaped(processes)
            prepared.reservation.validate()
            assert not held.cookies
        finally:
            prepared.reservation.release()


@pytest.mark.parametrize('kind', ['smtp', 'ntfy'])
@pytest.mark.parametrize('error', ['memory', 'enomem', 'io', 'import'])
def test_idna_preload_resource_failure_is_classified_before_readiness(monkeypatch, tmp_path, kind, error):
    target = smtp_supervisor.HELPER_PATH if kind == 'smtp' else ntfy_supervisor.HELPER
    helper = injected_helper(tmp_path, target, error, startup=True)
    asyncio.run(classified_attempt(monkeypatch, tmp_path, kind, helper, error, 'readiness'))


@pytest.mark.parametrize('kind,phase', [('smtp', 'tls'), ('smtp', 'connect'), ('smtp', 'auth'),
                                       ('ntfy', 'client'), ('ntfy', 'request')])
@pytest.mark.parametrize('error', ['memory', 'enomem', 'io', 'lookup', 'import', 'ssl'])
def test_transport_resource_errno_keeps_actual_phase_and_other_errors(monkeypatch, tmp_path, kind, phase, error):
    target = smtp_supervisor.HELPER_PATH if kind == 'smtp' else ntfy_supervisor.HELPER
    helper = injected_helper(tmp_path, target, error, phase=phase)
    asyncio.run(classified_attempt(monkeypatch, tmp_path, kind, helper, error, phase))


@pytest.mark.parametrize('kind', ['smtp', 'ntfy'])
@pytest.mark.parametrize('error', ['memory', 'enomem', 'io'])
def test_entry_setup_allocation_failure_is_safe_before_readiness(monkeypatch, tmp_path, kind, error):
    target = smtp_supervisor.HELPER_PATH if kind == 'smtp' else ntfy_supervisor.HELPER
    helper = injected_helper(tmp_path, target, error, entry=True)
    asyncio.run(classified_attempt(monkeypatch, tmp_path, kind, helper, error, 'readiness'))
