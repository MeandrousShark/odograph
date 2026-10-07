"""Unknown backend outcomes retain report admission until process recovery."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit


def test_real_connection_failure_is_sanitized_before_account_work(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from psycopg import AsyncConnection, OperationalError
    from app.account_context import AccountPrincipal
    from app.account_work import report_account_work
    from app.capacity import AdmissionManager
    from app.role_setup import RoleSetupError

    attempts = []

    async def fail_connect(cls, *args, **kwargs):
        attempts.append(cls)
        raise OperationalError('injected connection failure')

    monkeypatch.setattr(AsyncConnection, 'connect', classmethod(fail_connect))

    async def run():
        manager = AdmissionManager()
        pool = manager.manage_pool(SimpleNamespace(conninfo='postgresql://unused'), 'control')
        principal = AccountPrincipal(1, True, 1)
        async with manager.operation('foreground', principal) as owner:
            with pytest.raises(RoleSetupError, match='^database connection failed$'):
                async with report_account_work(pool, principal):
                    pytest.fail('account work began after connection failure')
            assert not owner._lifetime.lease_connection
            assert manager.snapshot()['leases'] == 0
        assert manager.snapshot()['foreground']['active'] == 0

    asyncio.run(run())
    assert len(attempts) == 1


@pytest.mark.parametrize('phase',['connect','setup','acquire','unlock','close'])
def test_unknown_lease_backend_fault_retains_owner_after_repeated_cancellation(phase):
    code = textwrap.dedent('''
        import asyncio, os
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from psycopg import OperationalError
        from app.account_context import AccountPrincipal
        from app.capacity import AdmissionManager
        import app.account_work as work
        PHASE = PHASE_VALUE
        async def run():
            manager = AdmissionManager()
            pool = manager.manage_pool(SimpleNamespace(), 'control')
            principal = AccountPrincipal(1,True,1)
            failed = asyncio.Event()
            class Cursor:
                async def fetchone(self): return (True,)
            class Conn:
                async def execute(self, query, *args):
                    phase = 'setup' if query.startswith('SET') else 'unlock' if 'unlock' in query else 'acquire'
                    if PHASE == phase:
                        failed.set()
                        raise OperationalError('injected unknown backend outcome')
                    return Cursor()
            @asynccontextmanager
            async def connection(pool):
                if PHASE == 'connect':
                    failed.set(); raise OperationalError('injected unknown connect outcome')
                yield Conn()
                if PHASE == 'close':
                    failed.set(); raise OperationalError('injected unknown close outcome')
            work._lease_connection = connection
            async def operation():
                async with manager.operation('foreground',principal):
                    async with work.report_account_work(pool,principal): pass
            task = asyncio.create_task(operation())
            await asyncio.wait_for(failed.wait(),1)
            await asyncio.sleep(.01)
            task.cancel(); await asyncio.sleep(.02); task.cancel(); await asyncio.sleep(.02)
            assert not task.done()
            assert manager.snapshot()['leases'] == 1
            assert manager.snapshot()['foreground']['active'] == 1
            print('retained',flush=True)
            os._exit(0)
        asyncio.run(run())
    ''').replace('PHASE_VALUE',repr(phase))
    # This injects raw wrapper faults; the real pre-lock connection path is above.
    # Process exit is the recovery boundary for an unknown established backend.
    result = subprocess.run([sys.executable,'-I','-c',
                             'import sys; sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');\n'+code],
                            capture_output=True,timeout=5,env={})
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'retained\n'
