"""Public command interface."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sys
import time

from . import __version__
from .config import ConfigError, load_config, public_plan
from .ssh import SSH
from .state import StateStore, default_state_dir, read_statuses
from .supervisor import supervise
from .ui import Display, frame


def parser():
    result = argparse.ArgumentParser(description="Watch OpenSSH tunnels with a fixed dashboard and automatic reconnects.")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="action", required=True)
    for name, help_text in [("run", "Connect and supervise SSH tunnel aliases"),
                            ("check", "Validate config and show inherited forwards; no SSH login")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("hosts", nargs="*", help="Configured tunnel names, or SSH aliases without --config")
        command.add_argument("--config", type=Path, help="JSON configuration file")
        if name == "run":
            command.add_argument("--state-dir", type=Path, default=default_state_dir())
            command.add_argument("--no-ui", action="store_true", help="Use line output instead of a fixed screen")
            command.add_argument("--duration", type=float, help="Stop after this many seconds (useful for smoke tests)")
    status = commands.add_parser("status", help="Read saved status; never start an SSH connection")
    status.add_argument("hosts", nargs="*")
    status.add_argument("--state-dir", type=Path, default=default_state_dir())
    status.add_argument("--watch", action="store_true", help="Display live status without owning the tunnels")
    status.add_argument("--json", action="store_true")
    return result


async def run(args, config):
    store = StateStore(args.state_dir)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
        signals.append(sig)
    try:
        with Display(enabled=not args.no_ui, log_path=store.directory / "events.jsonl") as display:
            await supervise(config, store, stop, display.update, args.duration)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        store.close()


async def check(config):
    plan = public_plan(config)
    for tunnel, row in zip(config.tunnels, plan["tunnels"]):
        row["forwards"] = await SSH(config.ssh_binary, tunnel, "/unused").describe()
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def status(args):
    if args.json and args.watch:
        raise ConfigError("Use either --json or --watch")
    if not args.watch:
        rows = read_statuses(args.state_dir, args.hosts)
        if args.json:
            print(json.dumps([asdict(s) for s in rows], ensure_ascii=False, indent=2))
        else:
            import shutil
            size = shutil.get_terminal_size((100, 30))
            print(frame(rows, size.columns, size.lines, str(args.state_dir / "events.jsonl")))
        return 0
    with Display(log_path=args.state_dir / "events.jsonl") as display:
        try:
            while True:
                display.update(read_statuses(args.state_dir, args.hosts))
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        if args.action == "status":
            return status(args)
        if args.action == "run" and args.duration is not None:
            from .config import number
            number(args.duration, "duration", 0.01)
        config = load_config(args.config, args.hosts)
        if args.action == "check":
            asyncio.run(check(config))
        else:
            asyncio.run(run(args, config))
        return 0
    except (ConfigError, ValueError, OSError, asyncio.TimeoutError) as exc:
        print(f"ssh-tunnel-watch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
