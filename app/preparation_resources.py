"""Cross-process spool reservations and charged, seekable preparation files."""
from __future__ import annotations

import fcntl
import io
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import time
from uuid import uuid4

OPERATION_BYTES = 512 * 1024 * 1024
INSTANCE_BYTES = 2 * 1024 * 1024 * 1024
METADATA_BYTES = 65536
BLOCK_BYTES = 4096
_COUNTER = struct.Struct('!QQ')
_SIZE = struct.Struct('!Q')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')


class PreparationBusy(RuntimeError):
    """Preparation cannot reserve or safely reclaim its finite spool budget."""


class PreparationResourceError(RuntimeError):
    """A preparation attempt exhausted its file or helper resource budget."""


class PreparationCleanupUnconfirmed(PreparationBusy):
    """A partial reservation still owns files and requires process recovery."""

    def __init__(self, reservation):
        super().__init__('preparation reservation cleanup is unconfirmed')
        self.reservation = reservation


def _identity(info):
    return info.st_dev, info.st_ino


def _open(name, flags, mode=0o600, *, directory=None):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     mode, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (28, 122):
            raise PreparationResourceError('preparation spool is unavailable') from None
        if exc.errno in (2, 20, 40):
            raise PreparationBusy('preparation filesystem handle is unavailable') from None
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise PreparationBusy('preparation requires current-user mode 0600 files')
    return fd


def _directory(name, *, parent=None):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PreparationBusy('preparation requires current-user mode 0700 directories')
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    if _identity(info) != _identity(os.fstat(fd)):
        os.close(fd)
        raise PreparationBusy('preparation directory changed during validation')
    return fd


def _private_directory(path):
    if path is None or path == '':
        path = Path(tempfile.gettempdir()) / f'odograph-preparation-{os.getuid()}'
    path = Path(path).absolute()
    try:
        path = path.parent.resolve(strict=True) / path.name
    except OSError:
        raise PreparationBusy('preparation spool parent is unavailable') from None
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise PreparationBusy('preparation spool is unavailable') from None
    return path, _directory(path)


def _lock(fd, deadline=None):
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                raise PreparationBusy('preparation registry wait expired') from None
            time.sleep(.01)


def _clear(directory):
    # Preparation creates only flat, registered files. Never traverse another tree.
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                raise PreparationBusy('preparation cleanup found an unexpected directory')
            os.unlink(entry.name, dir_fd=directory)


def _same_entry(parent, name, directory):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        raise PreparationBusy('preparation directory pathname changed') from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or _identity(info) != _identity(os.fstat(directory))):
        raise PreparationBusy('preparation directory pathname changed')


class SpoolReservation:
    def __init__(self, root, root_fd, name, directory_fd, guard):
        self.root, self.root_fd = root, root_fd
        self.name, self.directory_fd, self.guard = name, directory_fd, guard
        self.directory = root / name
        self.released = False
        self.failure_guard = None

    @classmethod
    def acquire(cls, root, deadline):
        root, root_fd = _private_directory(root)
        registry = None
        retained = False
        try:
            registry = _open('registry.lock', os.O_CREAT | os.O_RDWR, directory=root_fd)
            _lock(registry, deadline)
            _same_entry(None, root, root_fd)
            failure = _open('failure.lock', os.O_CREAT | os.O_RDWR, directory=root_fd)
            try:
                try:
                    fcntl.flock(failure, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise PreparationBusy('preparation cleanup remains unconfirmed') from None
            finally:
                os.close(failure)
            live = 0
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if entry.name in ('registry.lock', 'failure.lock'):
                        continue
                    if not re.fullmatch(r'op-[0-9a-f]{32}', entry.name):
                        raise PreparationBusy('preparation registry contains an unknown entry')
                    directory = _directory(entry.name, parent=root_fd)
                    guard = None
                    try:
                        guard = _open('owner.lock', os.O_CREAT | os.O_RDWR, directory=directory)
                        try:
                            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            live += OPERATION_BYTES
                            continue
                        _same_entry(root_fd, entry.name, directory)
                        _clear(directory)
                        _same_entry(root_fd, entry.name, directory)
                        os.rmdir(entry.name, dir_fd=root_fd)
                    except OSError:
                        raise PreparationBusy('preparation orphan cleanup is unconfirmed') from None
                    finally:
                        if guard is not None: os.close(guard)
                        os.close(directory)
            if live + OPERATION_BYTES > INSTANCE_BYTES:
                raise PreparationBusy('preparation instance reservation exhausted')
            _same_entry(None, root, root_fd)
            name = 'op-' + uuid4().hex
            directory = guard = None
            try:
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
                directory = _directory(name, parent=root_fd)
                guard = _open('owner.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, directory=directory)
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ResourceBudget.initialize(directory)
                _same_entry(None, root, root_fd)
                _same_entry(root_fd, name, directory)
            except BaseException:
                if directory is not None:
                    try:
                        _clear(directory)
                        _same_entry(root_fd, name, directory)
                        os.rmdir(name, dir_fd=root_fd)
                    except BaseException:
                        reservation = cls(root, root_fd, name, directory, guard)
                        reservation.failure_guard = _open('failure.lock', os.O_RDWR, directory=root_fd)
                        fcntl.flock(reservation.failure_guard, fcntl.LOCK_EX)
                        retained = True
                        raise PreparationCleanupUnconfirmed(reservation) from None
                    if guard is not None: os.close(guard)
                    os.close(directory)
                raise
            retained = True
            return cls(root, root_fd, name, directory, guard)
        finally:
            if registry is not None: os.close(registry)
            if not retained: os.close(root_fd)

    def validate(self):
        _same_entry(None, self.root, self.root_fd)
        _same_entry(self.root_fd, self.name, self.directory_fd)

    def release(self):
        if self.released:
            return
        registry = _open('registry.lock', os.O_RDWR, directory=self.root_fd)
        try:
            _lock(registry)
            self.validate()
            _clear(self.directory_fd)
            _same_entry(self.root_fd, self.name, self.directory_fd)
            os.rmdir(self.name, dir_fd=self.root_fd)
            if self.guard is not None: os.close(self.guard)
            os.close(self.directory_fd)
            self.released = True
        except (OSError, PreparationBusy):
            self.failure_guard = _open('failure.lock', os.O_RDWR, directory=self.root_fd)
            fcntl.flock(self.failure_guard, fcntl.LOCK_EX)
            raise PreparationBusy('preparation cleanup is unconfirmed') from None
        finally:
            os.close(registry)
            if self.released: os.close(self.root_fd)


class ResourceBudget:
    def __init__(self, directory, directory_fd=None, *, relative_paths=False):
        self.directory = Path(directory)
        self.relative_paths = relative_paths
        self.directory_fd = os.dup(directory_fd) if directory_fd is not None else _directory(directory)

    def close(self):
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def __del__(self):
        if getattr(self, 'directory_fd', None) is not None:
            self.close()

    @classmethod
    def initialize(cls, directory_fd):
        fd = _open('budget', os.O_CREAT | os.O_EXCL | os.O_RDWR, directory=directory_fd)
        try:
            os.write(fd, _COUNTER.pack(METADATA_BYTES, METADATA_BYTES))
        finally:
            os.close(fd)

    def path(self, name):
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in ('budget', 'owner.lock') or name.startswith('charge-'):
            raise ValueError('invalid preparation file name')
        if self.relative_paths:
            if _identity(os.stat('.')) != _identity(os.fstat(self.directory_fd)):
                raise PreparationBusy('preparation helper working directory changed')
            return Path(name)
        return self.directory / name

    def _change(self, difference):
        fd = _open('budget', os.O_RDWR, directory=self.directory_fd)
        try:
            _lock(fd)
            live, peak = _COUNTER.unpack(os.pread(fd, _COUNTER.size, 0))
            if live + difference > OPERATION_BYTES:
                raise PreparationResourceError('preparation spool reservation exhausted')
            if live + difference < METADATA_BYTES:
                raise PreparationResourceError('preparation accounting is invalid')
            live += difference
            os.pwrite(fd, _COUNTER.pack(live, max(live, peak)), 0)
        finally:
            os.close(fd)

    def usage(self):
        fd = _open('budget', os.O_RDONLY, directory=self.directory_fd)
        try:
            _lock(fd)
            return _COUNTER.unpack(os.pread(fd, _COUNTER.size, 0))
        finally:
            os.close(fd)

    def open(self, name, mode='w+b'):
        self.path(name)
        if mode in ('rb', 'r+b'):
            fd = _open(name, os.O_RDONLY if mode == 'rb' else os.O_RDWR, directory=self.directory_fd)
            if mode == 'rb':
                return os.fdopen(fd, 'rb')
            try:
                charge = _open('charge-' + name, os.O_RDWR, directory=self.directory_fd)
            except BaseException:
                os.close(fd)
                raise
        elif mode in ('wb', 'w+b'):
            self._change(BLOCK_BYTES)
            charge = None
            try:
                charge = _open('charge-' + name, os.O_CREAT | os.O_EXCL | os.O_RDWR, directory=self.directory_fd)
                os.write(charge, _SIZE.pack(0))
                fd = _open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, directory=self.directory_fd)
            except BaseException:
                if charge is not None: os.close(charge)
                raise
        else:
            raise ValueError('unsupported preparation file mode')
        return ChargedFile(fd, charge, self)

    def remove(self, name):
        self.path(name)
        charge = _open('charge-' + name, os.O_RDONLY, directory=self.directory_fd)
        try:
            size = _SIZE.unpack(os.pread(charge, _SIZE.size, 0))[0]
            os.unlink(name, dir_fd=self.directory_fd)
            os.unlink('charge-' + name, dir_fd=self.directory_fd)
            self._change(-(size + BLOCK_BYTES))
        finally:
            os.close(charge)

    def verify(self, name):
        path = self.path(name)
        info = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PreparationResourceError('invalid prepared file')
        charge = _open('charge-' + name, os.O_RDONLY, directory=self.directory_fd)
        try:
            size = _SIZE.unpack(os.pread(charge, _SIZE.size, 0))[0]
        finally:
            os.close(charge)
        if (info.st_size + BLOCK_BYTES - 1) // BLOCK_BYTES * BLOCK_BYTES > size:
            raise PreparationResourceError('prepared output has uncharged bytes')
        return path, info.st_size


class ChargedFile(io.RawIOBase):
    def __init__(self, fd, charge, budget):
        super().__init__()
        self.fd, self.charge, self.budget = fd, charge, budget

    def writable(self): return True
    def readable(self): return True
    def seekable(self): return True
    def fileno(self): return self.fd
    def tell(self): return os.lseek(self.fd, 0, os.SEEK_CUR)
    def seek(self, offset, whence=os.SEEK_SET): return os.lseek(self.fd, offset, whence)
    def read(self, size=-1):
        if size < 0: size = os.fstat(self.fd).st_size - self.tell()
        return os.read(self.fd, size)
    def readinto(self, buffer):
        data = self.read(len(buffer)); buffer[:len(data)] = data; return len(data)

    def write(self, data):
        size = memoryview(data).nbytes
        final = (self.tell() + size + BLOCK_BYTES - 1) // BLOCK_BYTES * BLOCK_BYTES
        previous = _SIZE.unpack(os.pread(self.charge, _SIZE.size, 0))[0]
        if final > previous:
            self.budget._change(final - previous)
            os.pwrite(self.charge, _SIZE.pack(final), 0)
        view = memoryview(data).cast('B')
        written = 0
        while written < size:
            written += os.write(self.fd, view[written:])
        return written

    def truncate(self, size=None):
        if size is None: size = self.tell()
        previous = self.tell()
        if size > os.fstat(self.fd).st_size:
            self.seek(size - 1); self.write(b'\0'); self.seek(previous)
        else:
            os.ftruncate(self.fd, size)
        return size

    def close(self):
        if not self.closed:
            os.close(self.fd); os.close(self.charge)
        super().close()
