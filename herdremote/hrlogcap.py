"""PodBox file-log budget (current plus one backup). No service configuration."""
import fcntl
import os
from pathlib import Path

TOTAL_BYTES = 5 * 1024 * 1024
PART_BYTES = TOTAL_BYTES // 2


def append(path, data, max_bytes=PART_BYTES):
    """Serialize across threads/processes on the persistent current inode.

    Copy-before-truncate leaves current available through a failed rotation.
    The backup is synced before truncation; no rename/unlink lock race exists.
    Limits supplied by tests may reduce, but cannot increase, the budget.
    """
    part = min(int(max_bytes), PART_BYTES)
    if part <= 0:
        raise ValueError('log limit must be positive')
    path = Path(path)
    backup = path.with_name(path.name + '.1')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        # Remove an oversized backup from an older version before writing.
        try:
            if backup.stat().st_size > part:
                backup.unlink()
        except FileNotFoundError:
            pass

        def rotate():
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - part))
            with backup.open('wb') as old:
                left = min(size, part)
                while left:
                    block = stream.read(min(left, 64 * 1024))
                    if not block:
                        raise OSError('short log rotation read')
                    old.write(block)
                    left -= len(block)
                old.flush()
                os.fsync(old.fileno())
            stream.seek(0)
            stream.truncate(0)
            stream.flush()

        if stream.seek(0, os.SEEK_END) > part:
            rotate()
        view = memoryview(data)
        for start in range(0, len(view), part):
            block = view[start:start + part]
            if stream.seek(0, os.SEEK_END) + len(block) > part:
                rotate()
            stream.write(block)
            stream.flush()
