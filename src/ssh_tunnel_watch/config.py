"""Strict, portable JSON configuration. Credentials remain in OpenSSH/SSH agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Optional


class ConfigError(ValueError):
    pass


def only_keys(value, allowed, context):
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f"Unknown {context} keys: {', '.join(sorted(unknown))}")


def number(value, label, minimum=0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(f"{label} must be finite and >= {minimum}")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{label} must be an integer")
    return value


def argv(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or "\0" in v for v in value):
        raise ConfigError(f"{label} must be an array of strings without NUL characters")
    if value and not value[0]:
        raise ConfigError(f"{label} executable cannot be empty")
    return tuple(value)


def text(value, label):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"{label} must be nonempty text without control characters")
    return value


@dataclass(frozen=True)
class Backoff:
    initial: float = 2
    factor: float = 2
    maximum: float = 120
    jitter: float = 0.2

    @classmethod
    def parse(cls, data):
        only_keys(data, cls.__dataclass_fields__, "backoff")
        obj = cls(**data)
        number(obj.initial, "backoff.initial", 0.01)
        number(obj.factor, "backoff.factor", 1)
        number(obj.maximum, "backoff.maximum", obj.initial)
        number(obj.jitter, "backoff.jitter")
        if obj.jitter > 1:
            raise ConfigError("backoff.jitter must be <= 1")
        return obj


@dataclass(frozen=True)
class Options:
    connect_timeout: int = 10
    startup_timeout: float = 30
    server_alive_interval: int = 15
    server_alive_count_max: int = 3
    check_interval: float = 20
    probe_timeout: float = 12
    probe_failures: int = 3
    stable_after: float = 60
    backoff: Backoff = field(default_factory=Backoff)

    @classmethod
    def parse(cls, data):
        only_keys(data, cls.__dataclass_fields__, "defaults/options")
        raw = dict(data)
        raw["backoff"] = Backoff.parse(raw.get("backoff", {}))
        obj = cls(**raw)
        for name in ["connect_timeout", "server_alive_interval", "server_alive_count_max", "probe_failures"]:
            number(getattr(obj, name), name, 1, integer=True)
        for name in ["startup_timeout", "check_interval", "probe_timeout", "stable_after"]:
            number(getattr(obj, name), name, 0.01)
        return obj


@dataclass(frozen=True)
class Notifications:
    command: tuple[str, ...] = ()
    after_failures: int = 5
    cooldown: float = 900
    timeout: float = 10
    recovery: bool = True

    @classmethod
    def parse(cls, data):
        only_keys(data, cls.__dataclass_fields__, "notify")
        raw = dict(data)
        raw["command"] = argv(raw.get("command", []), "notify.command")
        obj = cls(**raw)
        number(obj.after_failures, "notify.after_failures", 1, integer=True)
        number(obj.cooldown, "notify.cooldown", 0.01)
        number(obj.timeout, "notify.timeout", 0.01)
        if not isinstance(obj.recovery, bool):
            raise ConfigError("notify.recovery must be boolean")
        return obj


@dataclass(frozen=True)
class Tunnel:
    name: str
    host: str
    options: Options
    ssh_config: Optional[str] = None
    health_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    tunnels: tuple[Tunnel, ...]
    notify: Notifications = field(default_factory=Notifications)
    ssh_binary: str = "ssh"


def load_config(path: Optional[Path], hosts=()):
    raw = {}
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read JSON configuration: {path} ({type(exc).__name__})") from exc
    only_keys(raw, {"version", "defaults", "notify", "ssh_binary", "tunnels"}, "configuration")
    if type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
        raise ConfigError("configuration version must be 1")
    defaults = raw.get("defaults", {})
    Options.parse(defaults)
    entries = raw.get("tunnels", [])
    if not isinstance(entries, list):
        raise ConfigError("tunnels must be an array")
    tunnels = []
    for entry in entries:
        only_keys(entry, {"name", "host", "ssh_config", "health_command", "options"}, "tunnel")
        host = text(entry.get("host"), "tunnel.host")
        if host.startswith("-") or any(c.isspace() for c in host):
            raise ConfigError("tunnel.host must be an SSH alias or destination, not options")
        name = text(entry.get("name", host), "tunnel.name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ConfigError("tunnel.name must use 1–64 letters, numbers, dots, underscores or hyphens")
        overrides = entry.get("options", {})
        only_keys(overrides, Options.__dataclass_fields__, "tunnel.options")
        only_keys(overrides.get("backoff", {}), Backoff.__dataclass_fields__, "tunnel.options.backoff")
        merged = {**defaults, **overrides}
        merged["backoff"] = {**defaults.get("backoff", {}), **overrides.get("backoff", {})}
        ssh_config = entry.get("ssh_config")
        if ssh_config is not None:
            ssh_config = str(Path(text(ssh_config, "ssh_config")).expanduser())
            if path is not None and not Path(ssh_config).is_absolute():
                ssh_config = str((path.resolve().parent / ssh_config).resolve())
        tunnels.append(Tunnel(name, host, Options.parse(merged), ssh_config,
                              argv(entry.get("health_command", []), "health_command")))
    if len({t.name for t in tunnels}) != len(tunnels):
        raise ConfigError("tunnel names must be unique")
    if hosts:
        lookup = {t.name: t for t in tunnels}
        selected = []
        for name in hosts:
            if name in lookup:
                selected.append(lookup[name])
            elif path is None:
                # Apply exactly the same validation to ad-hoc aliases.
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                    raise ConfigError("Ad-hoc aliases need simple names; use a config for user@host")
                selected.append(Tunnel(name, name, Options.parse(defaults)))
            else:
                raise ConfigError(f"Unknown tunnel name: {name}")
        tunnels = list({t.name: t for t in selected}.values())
    if not tunnels:
        raise ConfigError("Specify SSH aliases, or provide a config containing tunnels")
    return Config(tuple(tunnels), Notifications.parse(raw.get("notify", {})),
                  text(raw.get("ssh_binary", "ssh"), "ssh_binary"))


def public_plan(config):
    """Deliberately omit notification arguments and SSH credential-related paths."""
    return {"tunnels": [{"name": t.name, "host": t.host,
                          "health": "remote command" if t.health_command else "SSH transport only",
                          "options": asdict(t.options)} for t in config.tunnels],
            "notify": {"enabled": bool(config.notify.command),
                       "after_failures": config.notify.after_failures,
                       "cooldown": config.notify.cooldown}}
