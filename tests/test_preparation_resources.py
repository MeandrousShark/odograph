from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.preparation_resources import (
    BLOCK_BYTES, METADATA_BYTES, OPERATION_BYTES, PreparationBusy,
    PreparationResourceError, ResourceBudget, SpoolReservation,
)

pytestmark = pytest.mark.unit


def reserve(tmp_path):
    return SpoolReservation.acquire(tmp_path / 'spool', time.monotonic() + 2)


def test_charges_seek_highwater_sidecar_and_removal(tmp_path):
    reservation = reserve(tmp_path)
    try:
        budget = ResourceBudget(reservation.directory)
        assert budget.usage() == (METADATA_BYTES, METADATA_BYTES)
        with budget.open('output') as output:
            output.write(b'a')
            output.seek(BLOCK_BYTES)
            output.write(b'b')
            output.seek(0)
            output.write(b'c')
            output.truncate(1)
        assert budget.usage() == (METADATA_BYTES + 3 * BLOCK_BYTES,) * 2
        assert budget.verify('output')[1] == 1
        budget.remove('output')
        assert budget.usage() == (METADATA_BYTES, METADATA_BYTES + 3 * BLOCK_BYTES)
    finally:
        reservation.release()


def test_growth_is_refused_before_the_file_changes(tmp_path):
    reservation = reserve(tmp_path)
    try:
        budget = ResourceBudget(reservation.directory)
        with budget.open('output') as output:
            output.seek(OPERATION_BYTES)
            with pytest.raises(PreparationResourceError): output.write(b'no')
            assert os.fstat(output.fileno()).st_size == 0
        assert budget.usage()[0] == METADATA_BYTES + BLOCK_BYTES
    finally:
        reservation.release()


def test_existing_charge_is_shared_between_budget_instances(tmp_path):
    reservation = reserve(tmp_path)
    try:
        first = ResourceBudget(reservation.directory)
        second = ResourceBudget(reservation.directory)
        with first.open('output') as sink: sink.write(b'a')
        with second.open('output', 'r+b') as sink:
            sink.seek(9000); sink.write(b'x')
        assert first.usage() == second.usage() == (METADATA_BYTES + 4 * BLOCK_BYTES,) * 2
        with second.open('output', 'rb') as source: assert source.read(1) == b'a'
    finally:
        reservation.release()


@pytest.mark.parametrize('name', ['../outside', '/outside', '.hidden', 'charge-x', 'budget', 'owner.lock', 'x'*97])
def test_file_names_cannot_escape_or_replace_accounting(tmp_path, name):
    reservation = reserve(tmp_path)
    try:
        with pytest.raises(ValueError): ResourceBudget(reservation.directory).open(name)
    finally:
        reservation.release()


def test_root_symlink_or_permissive_mode_is_refused(tmp_path):
    target = tmp_path / 'real'; target.mkdir(mode=0o700)
    link = tmp_path / 'link'; link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PreparationBusy): SpoolReservation.acquire(link, time.monotonic()+1)
    target.chmod(0o755)
    with pytest.raises(PreparationBusy): SpoolReservation.acquire(target, time.monotonic()+1)


def test_same_instance_four_grants_and_orphan_reclamation(tmp_path):
    root = tmp_path / 'spool'
    reservations = [SpoolReservation.acquire(root, time.monotonic()+2) for _ in range(4)]
    try:
        with pytest.raises(PreparationBusy): SpoolReservation.acquire(root, time.monotonic()+2)
        # Closing the only guard simulates confirmed process exit, not PID lookup.
        abandoned = reservations.pop()
        os.close(abandoned.guard)
        replacement = SpoolReservation.acquire(root, time.monotonic()+2)
        assert not abandoned.directory.exists()
        reservations.append(replacement)
    finally:
        for reservation in reservations: reservation.release()


def test_cleanup_failure_keeps_guard_and_refuses_new_admission(tmp_path, monkeypatch):
    reservation = reserve(tmp_path)
    from app import preparation_resources
    real = preparation_resources._clear
    def failure(path): raise OSError('simulated cleanup failure')
    monkeypatch.setattr('app.preparation_resources._clear', failure)
    with pytest.raises(PreparationBusy): reservation.release()
    assert not reservation.released
    with pytest.raises(PreparationBusy): reserve(tmp_path)
    # Test recovery explicitly releases the fatal process-lifetime guard.
    monkeypatch.setattr('app.preparation_resources._clear', real)
    os.close(reservation.failure_guard)
    reservation.failure_guard = None
    reservation.release()


def test_helper_inherited_guard_blocks_reclaim_after_parent_descriptor_close(tmp_path):
    reservation = reserve(tmp_path)
    process = subprocess.Popen([sys.executable, '-I', '-c',
        'import os,sys; print("ready",flush=True); os.read(0,1)', str(reservation.guard)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=(reservation.guard,), close_fds=True)
    try:
        assert process.stdout.readline() == b'ready\n'
        os.close(reservation.guard)
        replacement = SpoolReservation.acquire(reservation.root, time.monotonic()+2)
        assert reservation.directory.exists()
        replacement.release()
        process.kill(); process.wait(timeout=3)
        replacement = SpoolReservation.acquire(reservation.root, time.monotonic()+2)
        assert not reservation.directory.exists()
        replacement.release()
    finally:
        if process.poll() is None: process.kill(); process.wait()


def test_concurrent_processes_share_the_same_four_grants(tmp_path):
    script = tmp_path / 'child.py'
    script.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app.preparation_resources import SpoolReservation,PreparationBusy
try:
 r=SpoolReservation.acquire(sys.argv[2],time.monotonic()+3)
except PreparationBusy:
 print('busy',flush=True);sys.exit(3)
print('ready',flush=True)
sys.stdin.read(1)
r.release()
''')
    source = str(Path(__file__).resolve().parents[1])
    children = [subprocess.Popen([sys.executable, '-I', str(script), source, str(tmp_path/'spool')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE) for _ in range(5)]
    try:
        lines = [child.stdout.readline().strip() for child in children]
        assert lines.count(b'ready') == 4 and lines.count(b'busy') == 1
    finally:
        for child in children:
            if child.poll() is None: child.stdin.write(b'x'); child.stdin.flush()
        for child in children: child.wait(timeout=5)
    assert not list((tmp_path/'spool').glob('op-*'))


def test_empty_config_uses_stable_private_uid_root(tmp_path, monkeypatch):
    monkeypatch.setattr('app.preparation_resources.tempfile.tempdir',str(tmp_path))
    first=SpoolReservation.acquire('',time.monotonic()+2)
    second=SpoolReservation.acquire('',time.monotonic()+2)
    try:
        assert first.root==second.root==tmp_path/f'odograph-preparation-{os.getuid()}'
        assert first.directory != second.directory
        assert first.root.stat().st_mode&0o777==0o700
    finally:
        first.release();second.release()


def test_checked_operation_handle_cannot_follow_substituted_directory(tmp_path):
    reservation=reserve(tmp_path)
    budget=ResourceBudget(reservation.directory,reservation.directory_fd)
    moved=tmp_path/'retained-operation'
    outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    reservation.directory.rename(moved)
    reservation.directory.symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises(PreparationBusy):reservation.validate()
        with budget.open('output') as sink:sink.write(b'held directory')
        with budget.open('output','rb') as source:assert source.read()==b'held directory'
        assert (moved/'output').read_bytes()==b'held directory'
        assert not list(outside.iterdir())
    finally:
        reservation.directory.unlink();moved.rename(reservation.directory)
        reservation.release();budget.close()


def test_checked_root_handle_cannot_follow_substituted_root(tmp_path):
    reservation=reserve(tmp_path)
    budget=ResourceBudget(reservation.directory,reservation.directory_fd)
    moved=tmp_path/'retained-root'
    outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    reservation.root.rename(moved)
    reservation.root.symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises(PreparationBusy):reservation.validate()
        with budget.open('output') as sink:sink.write(b'held root')
        assert (moved/reservation.name/'output').read_bytes()==b'held root'
        assert not list(outside.iterdir())
    finally:
        reservation.root.unlink();moved.rename(reservation.root)
        reservation.release();budget.close()


def test_flat_orphan_cleanup_rejects_nested_directories(tmp_path):
    reservation=reserve(tmp_path)
    nested=reservation.directory/'unexpected';nested.mkdir()
    os.close(reservation.guard)
    try:
        with pytest.raises(PreparationBusy):reserve(tmp_path)
        assert nested.exists()
    finally:
        nested.rmdir()
        os.close(reservation.directory_fd);os.close(reservation.root_fd)
    replacement=reserve(tmp_path);replacement.release()


def test_partial_creation_cleanup_failure_retains_handles_and_blocks_admission(tmp_path,monkeypatch):
    from app import preparation_resources as resources
    real_initialize=resources.ResourceBudget.initialize
    real_clear=resources._clear
    def exhausted(*args):raise PreparationResourceError('metadata allocation exhausted')
    def cleanup_failure(*args):raise OSError('cleanup unconfirmed')
    monkeypatch.setattr(resources.ResourceBudget,'initialize',exhausted)
    monkeypatch.setattr(resources,'_clear',cleanup_failure)
    with pytest.raises(resources.PreparationCleanupUnconfirmed) as caught:reserve(tmp_path)
    reservation=caught.value.reservation
    assert not reservation.released and reservation.directory.exists()
    assert os.fstat(reservation.directory_fd) and os.fstat(reservation.guard)
    with pytest.raises(PreparationBusy):reserve(tmp_path)
    monkeypatch.setattr(resources.ResourceBudget,'initialize',real_initialize)
    monkeypatch.setattr(resources,'_clear',real_clear)
    os.close(reservation.failure_guard);reservation.failure_guard=None
    reservation.release()


def test_root_substitution_during_registry_wait_refuses_the_grant(tmp_path,monkeypatch):
    from app import preparation_resources as resources
    root=tmp_path/'spool';moved=tmp_path/'held-root';changed=False
    real_lock=resources._lock
    def substitute(fd,deadline=None):
        nonlocal changed
        real_lock(fd,deadline)
        if not changed:
            changed=True;root.rename(moved);root.mkdir(mode=0o700)
    monkeypatch.setattr(resources,'_lock',substitute)
    try:
        with pytest.raises(PreparationBusy):SpoolReservation.acquire(root,time.monotonic()+2)
        assert not list(root.iterdir()) and not list(moved.glob('op-*'))
    finally:
        root.rmdir();moved.rename(root)


def test_helper_relative_archive_paths_stay_inside_verified_cwd_after_substitution(tmp_path):
    reservation=reserve(tmp_path)
    budget=ResourceBudget(reservation.directory,reservation.directory_fd,relative_paths=True)
    previous=os.open('.',os.O_RDONLY|os.O_DIRECTORY)
    moved=tmp_path/'held-operation';outside=tmp_path/'outside';outside.mkdir(mode=0o700)
    try:
        os.fchdir(reservation.directory_fd)
        with budget.open('sheet.xml') as sink:sink.write(b'owned XML')
        reservation.directory.rename(moved);reservation.directory.symlink_to(outside,target_is_directory=True)
        (outside/'sheet.xml').write_bytes(b'outside data')
        assert budget.path('sheet.xml').read_bytes()==b'owned XML'
        assert budget.path('sheet.xml')==Path('sheet.xml')
        os.fchdir(previous)
        with pytest.raises(PreparationBusy):budget.path('sheet.xml')
    finally:
        os.fchdir(previous);os.close(previous)
        reservation.directory.unlink();moved.rename(reservation.directory)
        reservation.release();budget.close()
