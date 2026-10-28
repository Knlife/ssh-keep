"""One independent reconnect loop per tunnel, sharing only display and notifications."""
from __future__ import annotations

import asyncio
import random
import tempfile
import time
from pathlib import Path

from .notify import Notifier
from .ssh import SSH
from .state import Status


class RetryDelay:
    def __init__(self, options, rng=None):
        self.options = options
        self.value = 0
        self.rng = rng or random.Random()

    def next(self):
        o = self.options
        if not self.value:
            self.value = o.initial
        elif self.value >= o.maximum / o.factor:
            self.value = o.maximum
        else:
            self.value *= o.factor
        return max(0.01, min(o.maximum, self.value * (1 + self.rng.uniform(-o.jitter, o.jitter))))

    def reset(self):
        self.value = 0


class Worker:
    def __init__(self, config, tunnel, store, stop, ssh_factory=SSH):
        self.config = config
        self.tunnel = tunnel
        self.store = store
        self.stop = stop
        self.ssh_factory = ssh_factory
        self.status = Status(tunnel.name, tunnel.host,
                             health="COMMAND" if tunnel.health_command else "SSH")
        self.delay = RetryDelay(tunnel.options.backoff)
        self.notifier = Notifier(config.notify, store)
        self.notice_task = None
        self.last_alert = float("-inf")
        self.warned = False

    def event(self, event, **details):
        self.store.event(self.status, event, **details)

    def notify(self, event):
        if self.notice_task and not self.notice_task.done():
            return

        async def send():
            success = await self.notifier.send(self.status, event)
            if event == "failure" and success:
                self.warned = True

        self.notice_task = asyncio.create_task(send())

    async def session(self, ssh):
        s = self.status
        o = self.tunnel.options
        s.phase = "CONNECTING"
        s.attempts += 1
        s.retry_at = 0
        s.connected_at = 0
        s.probe_failures = 0
        self.event("connecting", attempt=s.attempts)
        await ssh.start()
        s.pid = ssh.process.pid
        self.store.save(s)
        deadline = time.monotonic() + o.startup_timeout
        ready = False
        while not self.stop.is_set() and time.monotonic() < deadline:
            if ssh.process.returncode is not None:
                await ssh.reader
                raise ConnectionError(ssh.last_error or f"SSH exited {ssh.process.returncode}")
            if await ssh.transport_ready():
                ready = True
                break
            await asyncio.sleep(0.2)
        if self.stop.is_set():
            return
        if not ready:
            raise ConnectionError("SSH startup timed out")
        s.connected_at = time.time()
        self.event("transport_ready")
        healthy_since = None
        stable = False
        while not self.stop.is_set():
            good, error = await ssh.probe()
            s.last_probe = time.time()
            if good:
                if healthy_since is None:
                    healthy_since = time.monotonic()
                s.probe_failures = 0
                s.error = ""
                next_phase = "HEALTHY" if self.tunnel.health_command else "SSH_READY"
                if s.phase != next_phase:
                    s.phase = next_phase
                    self.event("healthy", check=s.health)
                if not stable and time.monotonic() - healthy_since >= o.stable_after:
                    stable = True
                    previous = s.failures
                    s.failures = 0
                    self.delay.reset()
                    self.event("stable", previous_failures=previous)
                    self.last_alert = float("-inf")
                if stable and self.warned:
                    if not self.config.notify.recovery:
                        self.warned = False
                    elif not self.notice_task or self.notice_task.done():
                        self.notify("recovery")
                        self.warned = False
            else:
                healthy_since = None
                stable = False
                s.probe_failures += 1
                s.error = error
                s.phase = "DEGRADED"
                self.event("probe_failed", count=s.probe_failures, error=error)
                if s.probe_failures >= o.probe_failures:
                    raise ConnectionError(error)
            try:
                code = await asyncio.wait_for(ssh.process.wait(), o.check_interval)
            except asyncio.TimeoutError:
                continue
            await ssh.reader
            raise ConnectionError(ssh.last_error or f"SSH exited {code}")

    async def run(self):
        # Short paths avoid the ~104-byte Unix socket limit on macOS.
        with tempfile.TemporaryDirectory(prefix="stw-", dir="/tmp") as directory:
            path = Path(directory) / "control.sock"
            try:
                while not self.stop.is_set():
                    ssh = self.ssh_factory(self.config.ssh_binary, self.tunnel, path)
                    error = None
                    try:
                        await self.session(ssh)
                    except (OSError, ValueError, asyncio.TimeoutError) as exc:
                        from .state import safe_text
                        error = safe_text(str(exc)) or type(exc).__name__
                    finally:
                        await ssh.close()
                        self.status.pid = 0
                        path.unlink(missing_ok=True)
                    if self.stop.is_set():
                        break
                    s = self.status
                    s.failures += 1
                    s.error = error or "SSH session ended"
                    s.phase = "BACKOFF"
                    s.connected_at = 0
                    wait = self.delay.next()
                    s.retry_at = time.time() + wait
                    self.event("disconnected", failures=s.failures, error=s.error, retry_seconds=wait)
                    now = time.monotonic()
                    if s.failures >= self.config.notify.after_failures and now - self.last_alert >= self.config.notify.cooldown:
                        self.last_alert = now
                        self.notify("failure")
                    try:
                        await asyncio.wait_for(self.stop.wait(), wait)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if self.notice_task and not self.notice_task.done():
                    self.notice_task.cancel()
                if self.notice_task:
                    await asyncio.gather(self.notice_task, return_exceptions=True)
                self.status.phase = "STOPPED"
                self.status.pid = 0
                self.status.connected_at = 0
                self.status.retry_at = 0
                self.event("stopped")


async def supervise(config, store, stop, refresh, duration=None):
    workers = []
    for tunnel in config.tunnels:
        store.acquire(tunnel)
        worker = Worker(config, tunnel, store, stop)
        # Discover actual forwarding declarations before creating a connection.
        descriptor = SSH(config.ssh_binary, tunnel, "/unused")
        worker.status.forwards = await descriptor.describe()
        workers.append(worker)
    tasks = [asyncio.create_task(worker.run()) for worker in workers]
    deadline = time.monotonic() + duration if duration else None
    try:
        while not stop.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                stop.set()
                break
            for task in tasks:
                if task.done():
                    task.result()  # Surface a fatal worker error; never silently lose a host.
            for worker in workers:
                store.save(worker.status)
            refresh([w.status for w in workers])
            try:
                await asyncio.wait_for(stop.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        refresh([w.status for w in workers])
