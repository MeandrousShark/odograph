"""Actual supervised helper timezone configuration and memory authority."""
import asyncio
from pathlib import Path
import shutil
import sys
import zoneinfo

import pytest

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.preparation import PreparationOperation, PreparationResourceError
from app.report_preparation import Projection

pytestmark=pytest.mark.ops


def metadata():
    return dict(kind='range_html',start='2026-01-01',end='2026-01-31',user=dict(id=1,is_admin=False,has_avatar=False,avatar_version=0),
                csrf='token',csp_nonce='nonce',review_count=0,storage=None,request_path='/report/range')


def test_actual_helper_preserves_custom_operator_timezone_paths(tmp_path):
    utc=next((Path(path)/'UTC' for path in zoneinfo.TZPATH if (Path(path)/'UTC').is_file()),None)
    if utc is None:
        pytest.skip('installed UTC TZif unavailable')
    custom=tmp_path/'zones';(custom/'Custom').mkdir(parents=True)
    shutil.copyfile(utc,custom/'Custom'/'PreparationUnique')
    original=zoneinfo.TZPATH
    zoneinfo.reset_tzpath((str(custom),))
    assert zoneinfo.TZPATH == (str(custom),)
    try:
        assert zoneinfo.ZoneInfo('Custom/PreparationUnique').utcoffset(None).total_seconds()==0
        async def run():
            manager=AdmissionManager()
            async with manager.operation('foreground',AccountPrincipal(1,True,1)):
                async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                    async def prepare():
                        session=await operation.start_helper();projection=Projection(operation,session)
                        await projection.literal_text('email','custom@example.com')
                        await projection.literal_text('display_tz','Custom/PreparationUnique')
                        await projection.literal_text('app_version','test')
                        await projection.timezone_paths()
                        bounds=await session.request({'type':'initialize','metadata':metadata()})
                        assert bounds['start']=='2026-01-01T00:00:00+00:00'
                        await session.send_command({'type':'rate','row':None})
                        result=await session.request({'type':'render'})
                        await session.finish_input()
                        assert operation.budget.path(result['path']).is_file()
                    await operation.perform(prepare)
                    await operation.finish_preparation()
                    assert operation.process.returncode==0
                assert operation.closed and not operation.directory.exists()
        asyncio.run(run())
    finally:
        zoneinfo.reset_tzpath(original)
        zoneinfo.ZoneInfo.clear_cache(only_keys=['Custom/PreparationUnique'])


@pytest.mark.skipif(sys.platform!='linux',reason='hard AS authority is production Linux only')
def test_giant_timezone_paths_json_memory_failure_is_reaped(tmp_path):
    async def run():
        manager=AdmissionManager()
        async with manager.operation('foreground',AccountPrincipal(1,True,1)):
            async with PreparationOperation(spool_root=tmp_path/'spool') as operation:
                async def prepare():
                    session=await operation.start_helper();projection=Projection(operation,session)
                    await projection.literal_text('display_tz','UTC')
                    block=b'x'*16380
                    count=8192
                    await session.send_command({'type':'text','key':'timezone_paths','size':len(block)*count+5})
                    await session.send_text(b'["/')
                    for _ in range(count): await session.send_text(block)
                    await session.send_text(b'"]')
                    await session.request({'type':'initialize','metadata':metadata()})
                with pytest.raises(PreparationResourceError): await operation.perform(prepare)
                await asyncio.wait_for(asyncio.shield(operation.process.wait()),5)
            assert operation.closed and operation.process.returncode==73
            assert not operation.directory.exists()
    asyncio.run(run())
