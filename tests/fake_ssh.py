#!/usr/bin/env python3
"""A disposable subprocess peer for lifecycle/fault tests, never used in production."""
from pathlib import Path
import json
import os
import signal
import sys
import time

args = sys.argv[1:]
root = Path(os.environ["STW_FAKE_ROOT"])
if "-G" in args:
    print("hostname test.invalid\nremoteforward 11020 [localhost]:11020")
    raise SystemExit(0)
control = Path(args[args.index("-S") + 1])
if "-O" in args:
    try:
        pid = int(control.read_text())
        os.kill(pid, 0)
        raise SystemExit(0)
    except (OSError, ValueError):
        raise SystemExit(255)
if "-N" not in args:
    assert "ProxyCommand=false" in args, "Probe must not fall back to another connection"
    assert "ClearAllForwardings=yes" in args, "Probe must not recreate forwards"
    if (root / "probe-fail").exists():
        raise SystemExit(1)
    if (root / "probe-hang").exists():
        (root / "probe-pid").write_text(str(os.getpid()))
        time.sleep(60)
    raise SystemExit(0)

host = args[-1]
counter = root / (host + ".starts")
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
with (root / "starts.jsonl").open("a") as stream:
    stream.write(json.dumps({"host": host, "attempt": count, "time": time.time(), "pid": os.getpid()}) + "\n")
assert "ExitOnForwardFailure=yes" in args
assert "ControlPersist=no" in args
if host == "down" or (host == "flap" and count <= 2):
    print("remote port forwarding failed for listen port 11020", file=sys.stderr)
    raise SystemExit(255)

def stop(*_):
    control.unlink(missing_ok=True)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
control.write_text(str(os.getpid()))
while True:
    time.sleep(0.02)
