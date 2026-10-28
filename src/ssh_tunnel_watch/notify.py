"""Opt-in notification commands. No shell and no embedded credentials."""
from __future__ import annotations

import asyncio
import json
import os
import socket


class Notifier:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    async def send(self, status, event):
        from .ssh import terminate

        if not self.settings.command:
            status.last_notice = "disabled (configure notify.command)"
            self.store.event(status, "notification_disabled", notice=event)
            return False
        message = (f"SSH tunnel {status.name} ({status.host}) on {socket.gethostname()}: "
                   f"{event}; consecutive failures={status.failures}. "
                   "See ssh-tunnel-watch status and the private event log for details.")
        values = {"event": event, "host": status.host, "name": status.name,
                  "failures": str(status.failures), "message": message}
        args = []
        for original in self.settings.command:
            value = original
            for key, replacement in values.items():
                value = value.replace("{" + key + "}", replacement)
            args.append(os.path.expanduser(value) if not args else value)
        environment = dict(os.environ)
        environment.update({"STW_" + k.upper(): v for k, v in values.items()})
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, env=environment, start_new_session=True)
            await asyncio.wait_for(process.communicate(json.dumps(values).encode()), self.settings.timeout)
            if process.returncode:
                status.last_notice = f"{event}: command exited {process.returncode}"
                self.store.event(status, "notification_failed", notice=event, code=process.returncode)
                return False
            status.last_notice = event + ": sent"
            self.store.event(status, "notification_sent", notice=event)
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            status.last_notice = event + ": " + type(exc).__name__
            self.store.event(status, "notification_failed", notice=event, error=type(exc).__name__)
            return False
        finally:
            if process is not None:
                await terminate(process)
