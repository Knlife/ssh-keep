"""Private process locks, atomic snapshots and bounded event logs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time


@dataclass
class Status:
    name: str
    host: str
    phase: str = "STARTING"
    pid: int = 0
    attempts: int = 0
    failures: int = 0
    probe_failures: int = 0
    connected_at: float = 0
    retry_at: float = 0
    error: str = ""
    health: str = "SSH"
    last_probe: float = 0
    last_notice: str = ""
    updated_at: float = 0
    monitor_pid: int = 0
    forwards: tuple[str, ...] = ()


def default_state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base / "ssh-tunnel-watch"


def safe_text(value):
    # Remote stderr is untrusted: remove ANSI/OSC/control characters before display.
    import re
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(c for c in value if c.isprintable())[:500]


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.directory.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
            raise ValueError("State directory must be owned by you and not writable by others")
        os.chmod(self.directory, 0o700)
        self.locks = []
        self.logger = logging.Logger("ssh-tunnel-watch")
        handler = RotatingFileHandler(self.directory / "events.jsonl", maxBytes=2_000_000,
                                      backupCount=3, encoding="utf-8")
        os.chmod(self.directory / "events.jsonl", 0o600)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)

    def acquire(self, tunnel):
        key = hashlib.sha256((tunnel.host + "\0" + (tunnel.ssh_config or "")).encode()).hexdigest()[:20]
        # Reserve both target and display name, even when two different configs run.
        for name in ["host-" + key, "name-" + tunnel.name]:
            stream = (self.directory / (name + ".lock")).open("a+")
            os.chmod(stream.name, 0o600)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise ValueError(f"Tunnel {tunnel.name} is already managed in this state directory")
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
            self.locks.append(stream)

    def save(self, status):
        status.updated_at = time.time()
        status.monitor_pid = os.getpid()
        path = self.directory / (status.name + ".json")
        stage = path.with_suffix(".json.tmp")
        with stage.open("w", encoding="utf-8") as stream:
            os.chmod(stage, 0o600)
            json.dump(asdict(status), stream, ensure_ascii=False)
        os.replace(stage, path)

    def event(self, status, event, **details):
        self.logger.info(json.dumps({"time": time.time(), "name": status.name,
                                    "host": status.host, "event": event, **details}, ensure_ascii=False))
        self.save(status)

    def close(self):
        for stream in self.locks:
            stream.close()
        self.locks.clear()
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


def read_statuses(directory, names=()):
    rows = []
    for path in sorted(Path(directory).expanduser().glob("*.json")):
        if names and path.stem not in names:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            allowed = Status.__dataclass_fields__
            row = Status(**{k: v for k, v in value.items() if k in allowed})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if row.phase not in {"STOPPED", "STALE"}:
            try:
                os.kill(row.monitor_pid, 0)
                alive = row.monitor_pid > 0
            except (OSError, ValueError):
                alive = False
            if not alive or time.time() - row.updated_at > 10:
                row.phase = "STALE"
                row.pid = 0
        rows.append(row)
    return rows
