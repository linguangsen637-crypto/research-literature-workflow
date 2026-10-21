"""Linux descriptor-relative filesystem operations; no symlink traversal."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid
from .core import SafetyError, digest

ANY = object()


def parts(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise SafetyError('absolute canonical path required')
    return path.parts[1:]


@contextmanager
def directory(path, create=False):
    """Open from filesystem root, refusing symlinks at every component."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts(path):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def attached(path, fd):
    with directory(path) as fresh:
        a, b = os.fstat(fd), os.fstat(fresh)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise SafetyError('directory changed during operation')


def read_at(fd, name):
    try:
        filefd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(filefd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SafetyError('regular non-hardlinked file required')
        chunks = []
        while True:
            data = os.read(filefd, 1024 * 1024)
            if not data:
                break
            chunks.append(data)
        after = os.fstat(filefd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SafetyError('file changed during read')
        return b''.join(chunks)
    finally:
        os.close(filefd)


def read(path):
    path = Path(path)
    try:
        with directory(path.parent) as fd:
            data = read_at(fd, path.name)
            attached(path.parent, fd)
            return data
    except FileNotFoundError:
        return None


def write(path, data, create_only=False, expected=ANY):
    """Atomic publishing. Existing-file updates additionally require a quiescent editor."""
    path = Path(path)
    temporary = '.research-os-' + uuid.uuid4().hex
    with directory(path.parent, create=True) as fd:
        filefd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        try:
            with os.fdopen(filefd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            attached(path.parent, fd)
            current = read_at(fd, path.name)
            current_hash = digest(current) if current is not None else None
            if expected is not ANY and expected != current_hash:
                raise SafetyError('concurrent modification before publication')
            if create_only:
                os.link(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            else:
                os.replace(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            attached(path.parent, fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
            os.fsync(fd)


def unlink(path):
    path = Path(path)
    with directory(path.parent) as fd:
        attached(path.parent, fd)
        os.unlink(path.name, dir_fd=fd)
        os.fsync(fd)
