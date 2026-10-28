"""OpenSSH lifecycle and multiplexed checks; never interpret local shell strings."""
from __future__ import annotations

import asyncio
import os
import shlex
import signal

from .state import safe_text


async def terminate(process, grace=2):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def capture(command, timeout, input_data=None):
    """Bound time/output; cancelled probes must not leave an SSH/hook child behind."""
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def communicate():
        chunks = []
        size = 0
        if input_data is not None:
            try:
                process.stdin.write(input_data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            if size < 8192:
                chunks.append(chunk[:8192 - size])
                size += len(chunks[-1])
        await process.wait()
        return process.returncode, b"".join(chunks).decode(errors="replace")

    try:
        return await asyncio.wait_for(communicate(), timeout)
    finally:
        await terminate(process)


class SSH:
    def __init__(self, binary, tunnel, control_path):
        self.binary = binary
        self.tunnel = tunnel
        self.control_path = str(control_path)
        self.process = None
        self.reader = None
        self.last_error = ""

    def base(self):
        result = [self.binary]
        if self.tunnel.ssh_config:
            result += ["-F", self.tunnel.ssh_config]
        return result

    def command(self):
        o = self.tunnel.options
        result = self.base() + ["-n", "-N", "-T", "-M", "-S", self.control_path]
        options = {"BatchMode": "yes", "ExitOnForwardFailure": "yes", "ControlMaster": "yes",
                   "ControlPersist": "no", "ForkAfterAuthentication": "no", "RemoteCommand": "none",
                   "RequestTTY": "no", "PermitLocalCommand": "no", "LogLevel": "ERROR",
                   "ConnectTimeout": str(o.connect_timeout), "ConnectionAttempts": "1",
                   "ServerAliveInterval": str(o.server_alive_interval),
                   "ServerAliveCountMax": str(o.server_alive_count_max)}
        for key, value in options.items():
            result += ["-o", key + "=" + value]
        return result + [self.tunnel.host]

    async def describe(self):
        code, output = await capture(self.base() + ["-G", self.tunnel.host], 8)
        if code:
            raise ValueError(f"OpenSSH cannot read configuration for {self.tunnel.name}")
        forwards = []
        for line in output.splitlines():
            key, _, value = line.partition(" ")
            if key in {"remoteforward", "localforward", "dynamicforward"}:
                forwards.append(safe_text(key + " " + value))
        return tuple(forwards)

    async def start(self):
        self.last_error = ""
        self.process = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.reader = asyncio.create_task(self.read_errors())

    async def read_errors(self):
        pending = b""
        while True:
            chunk = await self.process.stderr.read(2048)
            if not chunk:
                break
            pending = (pending + chunk)[-8192:]
            if b"\n" in pending:
                lines = pending.split(b"\n")
                for line in lines[:-1]:
                    if line.strip():
                        self.last_error = safe_text(line.decode(errors="replace"))
                pending = lines[-1]
        if pending.strip():
            self.last_error = safe_text(pending.decode(errors="replace"))

    async def transport_ready(self):
        code, _ = await capture(self.base() + ["-S", self.control_path, "-O", "check",
                                               "-o", "BatchMode=yes", self.tunnel.host], 3)
        return code == 0

    async def probe(self):
        if not await self.transport_ready():
            return False, "SSH control channel unavailable"
        if not self.tunnel.health_command:
            return True, ""
        # A failed mux MUST NOT silently create a separate SSH connection. Existing
        # mux clients do not evaluate ProxyCommand, while a fallback exits immediately.
        command = self.base() + ["-S", self.control_path, "-T", "-o", "ControlMaster=no",
                                "-o", "ControlPersist=no", "-o", "ProxyCommand=false",
                                "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes",
                                "-o", "RemoteCommand=none", "-o", "PermitLocalCommand=no",
                                self.tunnel.host, shlex.join(self.tunnel.health_command)]
        try:
            code, _ = await capture(command, self.tunnel.options.probe_timeout)
            return code == 0, "" if code == 0 else f"Remote health command exited {code}"
        except asyncio.TimeoutError:
            return False, "Remote health command timed out"

    async def close(self):
        if self.process is not None:
            await terminate(self.process)
        if self.reader is not None:
            await self.reader
