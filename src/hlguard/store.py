from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any


@contextmanager
def locked(path: Path, mode: str) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    file = os.fdopen(fd, mode)
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        yield file
    finally:
        file.flush()
        try:
            os.fsync(file.fileno())
        except OSError:
            pass
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with locked(path, "r+") as file:
        raw = file.read()
    if not raw.strip():
        return default
    return json.loads(raw)


def update_json(path: Path, default: Any, mutator: Callable[[Any], Any]) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path, "r+") as file:
        raw = file.read()
        data = json.loads(raw) if raw.strip() else default
        updated = mutator(data)
        file.seek(0)
        file.truncate()
        file.write(json.dumps(updated, indent=2, sort_keys=True))
        return updated
