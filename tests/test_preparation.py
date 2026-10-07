from __future__ import annotations

import asyncio
import os
import struct
import textwrap
import time

import pytest

from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager, CapacityContractError
from app.preparation import (
    FRAME_BYTES, PreparationError, PreparationOperation, PreparationResourceError, _json_frame,
)

pytestmark = pytest.mark.unit


async def owned(function):
    manager = AdmissionManager()
    async with manager.operation('foreground', AccountPrincipal(1, True, 1)) as owner:
        return await function(owner)


def helper(tmp_path, monkeypatch, body):
    path = tmp_path/'helper.py'
    path.write_text('''import os,sys,struct,json,time
H=struct.Struct('!cI')
def read(n):
 data=b''
 while len(data)<n:
  part=os.read(0,n-len(data))
  if not part: raise EOFError()
  data+=part
 return data
def receive():
 kind,n=H.unpack(read(5));return kind,read(n)
def reply(value):
 raw=json.dumps(value).encode();os.write(1,H.pack(b'R',len(raw))+raw)
os.write(1,b'PREP1 READY\\n')
receive()
''' + textwrap.dedent(body))
    monkeypatch.setattr('app.preparation._HELPER', path)


def test_requires_admitted_foreground_or_background(tmp_path):
    async def run():
        with pytest.raises(CapacityContractError):
            async with PreparationOperation(spool_root=tmp_path/'spool'): pass
    asyncio.run(run())


def test_frame_budget_is_checked_without_encoding_giant_values():
    assert _json_frame({'a': [1, 'é', True]}) == '{"a":[1,"é",true]}'.encode()
    with pytest.raises(PreparationResourceError): _json_frame({'a': '😀' * (FRAME_BYTES+1)})
    with pytest.raises(PreparationResourceError): _json_frame({'a': '😀' * 20000})
    with pytest.raises(PreparationResourceError): _json_frame({'a': [0] * 257})
    with pytest.raises(ValueError): _json_frame({'a': object()})


def test_deadline_cancels_lease_or_query_wait_once_and_drains_cleanup(tmp_path):
    async def run(owner):
        started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellations = 0
        async def work():
            nonlocal cancellations
            started.set()
            try: await asyncio.Future()
            except asyncio.CancelledError:
                cancellations += 1; cleanup.set(); await release.wait(); raise
        async with PreparationOperation(spool_root=tmp_path/'spool', timeout_s=.5) as op:
            task = asyncio.create_task(op.perform(work))
            await started.wait(); await cleanup.wait()
            task.cancel(); task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not owner._lifetime.released
            assert op.directory.exists() and cancellations == 1
            release.set()
            with pytest.raises(PreparationResourceError): await task
        assert op.closed and not op.directory.exists()
    asyncio.run(owned(run))


def test_cancelled_reservation_is_reclaimed_before_owner_release(tmp_path, monkeypatch):
    from app.preparation_resources import SpoolReservation
    acquire = SpoolReservation.acquire
    import threading
    began, release = threading.Event(), threading.Event()
    def slow(*args):
        began.set();release.wait(3);return acquire(*args)
    monkeypatch.setattr('app.preparation.SpoolReservation.acquire', slow)
    async def run(owner):
        async def enter():
            async with PreparationOperation(spool_root=tmp_path/'spool'): pass
        task=asyncio.create_task(enter())
        while not began.is_set():
            assert not task.done()
            await asyncio.sleep(.001)
        task.cancel();await asyncio.sleep(.01)
        assert not task.done() and not owner._lifetime.released
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert not list((tmp_path/'spool').glob('op-*'))
    asyncio.run(owned(run))


def test_rpc_readiness_and_no_database_authority(tmp_path, monkeypatch):
    helper(tmp_path,monkeypatch,'''\
assert 'DATABASE_URL' not in os.environ
allowed={0,1,2,int(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5])}
for fd in range(128):
 try:os.fstat(fd)
 except OSError:continue
 assert fd in allowed
kind,payload=receive();assert kind==b'C'
reply({'value':json.loads(payload)['value']})
kind,payload=receive();assert kind==b'T'
reply({'bytes':len(payload)})
''')
    monkeypatch.setenv('DATABASE_URL','postgres://not-for-the-helper')
    async def run(owner):
        unrelated=os.open(__file__,os.O_RDONLY)
        try:
            async with PreparationOperation(spool_root=tmp_path/'spool') as op:
                async def prepare():
                    session=await op.start_helper()
                    assert await session.request({'value':17}) == {'value':17}
                    await session.send_text(b'\xc3\xa9')
                    assert await session.response() == {'bytes':2}
                    await session.finish_input()
                await op.perform(prepare)
                await op.finish_preparation()
                assert op.process.returncode == 0 and op.finished
        finally:os.close(unrelated)
    asyncio.run(owned(run))


def test_helper_unexpected_exit_has_no_success(tmp_path, monkeypatch):
    helper(tmp_path,monkeypatch,'sys.exit(73)\n')
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            session=await op.start_helper()
            with pytest.raises(PreparationResourceError): await session.response()
        assert op.process.returncode == 73
    asyncio.run(owned(run))


def test_response_survives_preparation_deadline_then_cleans_on_send_failure(tmp_path):
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool',timeout_s=.5) as op:
            with op.budget.open('result') as output: output.write(b'x'*70000)
            await op.finish_preparation()
            response=op.prepared('result',media_type='text/plain',filename='result.txt')
            await asyncio.sleep(.55)
            assert not op.stopped
            messages=[]
            async def send(message):
                messages.append(message)
                if message['type']=='http.response.body': raise RuntimeError('client disconnected')
            with pytest.raises(RuntimeError): await response({},None,send)
            assert dict(messages[0]['headers'])[b'content-length'] == b'70000'
            assert len(messages[1]['body']) == 65536
            assert op.closed and not op.directory.exists()
    asyncio.run(owned(run))


def test_cancelled_response_retains_files_until_send_cleanup(tmp_path):
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            with op.budget.open('result') as output: output.write(b'x')
            await op.finish_preparation()
            response=op.prepared('result',media_type='text/plain')
            sending=asyncio.Event()
            async def send(message):
                if message['type']=='http.response.body':sending.set();await asyncio.Future()
            task=asyncio.create_task(response({},None,send));await sending.wait();task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert op.closed and not op.directory.exists()
    asyncio.run(owned(run))


def test_hung_helper_killed_and_reaped_through_repeated_cleanup_cancellation(tmp_path, monkeypatch):
    helper(tmp_path,monkeypatch,'''\
import signal
signal.signal(signal.SIGTERM,signal.SIG_IGN)
reply({'ready':True})
while True:time.sleep(1)
''')
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            session=await op.start_helper()
            assert await session.response() == {'ready':True}
            task=asyncio.create_task(op.close())
            await asyncio.sleep(.1)
            task.cancel();await asyncio.sleep(.1);task.cancel()
            assert not task.done() and op.directory.exists()
            with pytest.raises(asyncio.CancelledError):await task
            assert op.process.returncode is not None and op.closed
            assert not op.directory.exists()
    asyncio.run(owned(run))


def test_real_helper_timezone_bounds_before_history_queries(tmp_path):
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            session=await op.start_helper()
            await session.send_command({'type':'text','key':'display_tz','size':3})
            await session.send_text(b'UTC')
            result=await session.request({'type':'initialize','metadata':{'kind':'annual_html','start':'2026-01-01','end':'2026-12-31'}})
            assert result['start']=='2026-01-01T00:00:00+00:00'
            assert result['next_year_start']=='2027-01-01T00:00:00+00:00'
        assert op.process.returncode is not None and op.closed
    asyncio.run(owned(run))


def test_unconfirmed_client_cleanup_keeps_operation_until_process_recovery(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from app.preparation_resources import SpoolReservation
    script=tmp_path/'unconfirmed.py'
    script.write_text('''import asyncio,os,sys
sys.path.insert(0,sys.argv[1])
from psycopg import OperationalError
from app.account_context import AccountPrincipal
from app.capacity import AdmissionManager
from app.preparation import PreparationOperation
state={}
async def job():
 async with AdmissionManager().operation('foreground',AccountPrincipal(1,True,1)) as owner:
  state['owner']=owner
  async with PreparationOperation(spool_root=sys.argv[2]) as op:
   state['op']=op
   async def failed():
    try:raise OperationalError('connection unavailable')
    except OperationalError as exc:
     await op.backend_failure(exc)
     raise
   await op.perform(failed)
async def main():
 task=asyncio.create_task(job())
 await asyncio.sleep(.1)
 task.cancel();await asyncio.sleep(.05);task.cancel();await asyncio.sleep(.05)
 assert not task.done() and not state['owner']._lifetime.released
 assert state['op'].directory.exists() and not state['op'].closed
 print('owned until recovery',flush=True)
 os._exit(0)
asyncio.run(main())
''')
    root=tmp_path/'spool'
    process=subprocess.run([sys.executable,'-I',str(script),str(Path(__file__).resolve().parents[1]),str(root)],capture_output=True,timeout=5)
    assert process.returncode==0 and process.stdout==b'owned until recovery\n'
    assert len(list(root.glob('op-*')))==1
    replacement=SpoolReservation.acquire(root,time.monotonic()+2)
    assert len(list(root.glob('op-*')))==1
    replacement.release()


def test_helper_resource_stop_before_readiness_is_a_resource_failure(tmp_path, monkeypatch):
    path=tmp_path/'early.py';path.write_text('import os;os._exit(73)\n')
    monkeypatch.setattr('app.preparation._HELPER',path)
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            with pytest.raises(PreparationResourceError): await op.start_helper()
        assert op.closed and op.process.returncode==73
    asyncio.run(owned(run))


def test_parent_sampled_memory_stop_kills_child_and_drains_reap(tmp_path, monkeypatch):
    helper(tmp_path,monkeypatch,'while True:time.sleep(1)\n')
    monkeypatch.setattr('app.preparation._resident_bytes',lambda pid:257*1024*1024)
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            op._sampled_rss=True
            async def work():
                session=await op.start_helper()
                await session.response()
            with pytest.raises(PreparationResourceError):await op.perform(work)
        assert op._resource_stop and op.process.returncode is not None and op.closed
    asyncio.run(owned(run))


def test_explicit_disconnect_stop_cancels_without_reclassifying_resource_failure(tmp_path):
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            began=asyncio.Event()
            async def work():began.set();await asyncio.Future()
            task=asyncio.create_task(op.perform(work));await began.wait();op.stop()
            with pytest.raises(asyncio.CancelledError):await task
            assert op._stop_reason=='cancel'
        assert op.closed
    asyncio.run(owned(run))


def test_default_operation_prepares_without_explicit_spool_config(tmp_path, monkeypatch):
    monkeypatch.setattr('app.preparation_resources.tempfile.tempdir',str(tmp_path))
    async def run(owner):
        async with PreparationOperation(spool_root='') as op:
            with op.budget.open('result') as output:output.write(b'default root')
            await op.finish_preparation()
            response=op.prepared('result',media_type='text/plain')
            messages=[]
            async def send(message):messages.append(message)
            await response({},None,send)
            assert b''.join(m.get('body',b'') for m in messages)==b'default root'
        assert op.closed and op.reservation.root==tmp_path/f'odograph-preparation-{os.getuid()}'
    asyncio.run(owned(run))


def test_response_timeout_drains_actual_stalled_read_before_files_and_owner_release(tmp_path, monkeypatch):
    import threading
    began, release=threading.Event(), threading.Event()
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            with op.budget.open('result') as output:output.write(b'x')
            await op.finish_preparation()
            real_open=op.budget.open
            class Stalled:
                def __init__(self,stream):self.stream=stream
                def read(self,size):began.set();release.wait(3);return self.stream.read(size)
                def close(self):self.stream.close()
            monkeypatch.setattr(op.budget,'open',lambda *a,**kw:Stalled(real_open(*a,**kw)))
            response=op.prepared('result',media_type='text/plain')
            messages=[]
            async def send(message):messages.append(message)
            task=asyncio.create_task(response({'state':{'_capacity_response_deadline':time.monotonic()+1.0}},None,send))
            while not began.is_set():
                assert not task.done()
                await asyncio.sleep(.001)
            await asyncio.sleep(1.05)
            assert not task.done() and not owner._lifetime.released and op.directory.exists()
            assert len(messages)==1
            release.set()
            with pytest.raises(TimeoutError):await task
            assert op.closed and not op.directory.exists()
    asyncio.run(owned(run))


def test_response_timeout_drains_actual_stalled_open_and_closes_returned_file(tmp_path, monkeypatch):
    import threading
    began, release=threading.Event(), threading.Event()
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            with op.budget.open('result') as output:output.write(b'x')
            await op.finish_preparation()
            real_open=op.budget.open;opened=[]
            def slow(*a,**kw):
                began.set();release.wait(3);stream=real_open(*a,**kw);opened.append(stream);return stream
            monkeypatch.setattr(op.budget,'open',slow)
            response=op.prepared('result',media_type='text/plain')
            messages=[]
            async def send(message):messages.append(message)
            task=asyncio.create_task(response({'state':{'_capacity_response_deadline':time.monotonic()+1.0}},None,send))
            while not began.is_set():
                assert not task.done()
                await asyncio.sleep(.001)
            await asyncio.sleep(1.05);task.cancel();task.cancel()
            assert not task.done() and op.directory.exists() and not messages
            release.set()
            with pytest.raises(asyncio.CancelledError):await task
            assert opened[0].closed and op.closed and not op.directory.exists()
    asyncio.run(owned(run))


def test_helper_launch_rejects_substituted_operation_path(tmp_path):
    from app.preparation_resources import PreparationBusy
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            moved=tmp_path/'held';outside=tmp_path/'outside';outside.mkdir(mode=0o700)
            op.directory.rename(moved);op.directory.symlink_to(outside,target_is_directory=True)
            try:
                with pytest.raises(PreparationBusy):await op.start_helper()
                assert op._spawn is None and not list(outside.iterdir())
            finally:
                op.directory.unlink();moved.rename(op.directory)
        assert op.closed
    asyncio.run(owned(run))


@pytest.mark.parametrize('fault',['quota','enospc'])
def test_resource_failure_during_text_input_is_503_class_even_before_result_read(tmp_path,monkeypatch,fault):
    from app import preparation
    helper_path=tmp_path/'quota-helper.py'
    helper_path.write_text('''import runpy,sys,types,errno
module=runpy.run_path(sys.argv[6])
def render(channel,budget):
 channel.recv_text()
 if sys.argv[7]=='enospc':raise OSError(errno.ENOSPC,'disk full')
 from app.preparation_resources import OPERATION_BYTES
 with budget.open('overflow') as sink:
  sink.seek(OPERATION_BYTES);sink.write(b'x')
sys.modules['app.report_renderer']=types.SimpleNamespace(render=render)
sys.exit(module['main']())
''')
    # The fixture script receives the real entrypoint and mode in its source,
    # keeping the production helper's fixed argv protocol unchanged.
    real_path=preparation._HELPER
    text=helper_path.read_text().replace('sys.argv[6]',repr(str(real_path))).replace("sys.argv[7]",repr(fault))
    helper_path.write_text(text)
    monkeypatch.setattr(preparation,'_HELPER',helper_path)
    async def run(owner):
        async with PreparationOperation(spool_root=tmp_path/'spool') as op:
            session=await op.start_helper()
            async def feed():
                for _ in range(20):await session.send_text(b'x'*FRAME_BYTES)
                await session.response()
            with pytest.raises(PreparationResourceError):await op.perform(feed)
        assert op.process.returncode==73 and op.closed and not op.directory.exists()
    asyncio.run(owned(run))
