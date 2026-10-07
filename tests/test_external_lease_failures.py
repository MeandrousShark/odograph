"""Unknown external lease outcomes retain real admission until process recovery."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.ops


@pytest.mark.parametrize('lane', ['background', 'mail'])
@pytest.mark.parametrize('phase', ['setup', 'acquire', 'commit', 'close'])
def test_uncertain_external_lease_retains_owner_after_repeated_cancel(lane, phase):
    code = textwrap.dedent('''
        import asyncio, os
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from psycopg import OperationalError
        from app.account_context import AccountPrincipal
        from app.capacity import AdmissionManager
        import app.account_work as work
        async def run():
            manager = AdmissionManager()
            pool = manager.manage_pool(SimpleNamespace(), 'control')
            failed = asyncio.Event()
            class Conn:
                @asynccontextmanager
                async def transaction(self):
                    yield
                    if PHASE == 'commit':
                        failed.set()
                        raise OperationalError('injected uncertain transaction end')
                async def execute(self, query, *args):
                    step = 'setup' if query.startswith('SET') else 'acquire'
                    if PHASE == step:
                        failed.set()
                        raise OperationalError('injected uncertain query')
            @asynccontextmanager
            async def connection(pool):
                yield Conn()
                if PHASE == 'close':
                    failed.set()
                    raise OperationalError('injected uncertain close')
            work._lease_connection = connection
            async def operation():
                async with manager.operation(LANE, principal=AccountPrincipal(1,True,1)):
                    async with work.external_account_work(pool, 1):
                        pass
            task = asyncio.create_task(operation())
            await asyncio.wait_for(failed.wait(), 1)
            await asyncio.sleep(.01)
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            assert manager.snapshot()['leases'] == 1
            assert manager.snapshot()[LANE]['active'] == 1
            print('retained', flush=True)
            os._exit(0)
        asyncio.run(run())
    ''')
    source = 'import sys; sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ')\n'
    source += 'PHASE=' + repr(phase) + '\nLANE=' + repr(lane) + '\n' + code
    result = subprocess.run([sys.executable, '-I', '-c', source],
                            capture_output=True, timeout=5, env={})
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b'retained\n'
