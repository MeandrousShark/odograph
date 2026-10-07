"""Unknown backend outcomes retain report admission until process recovery."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit


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
    # The process exit is the declared recovery boundary for an unknown backend.
    result = subprocess.run([sys.executable,'-I','-c',
                             'import sys; sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');\n'+code],
                            capture_output=True,timeout=5,env={})
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'retained\n'
