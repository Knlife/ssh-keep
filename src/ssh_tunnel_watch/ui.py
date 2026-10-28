"""A fixed terminal dashboard, with a non-interactive line-output fallback."""
from __future__ import annotations

import os
import shutil
import sys
import time
import unicodedata

from .state import safe_text


def width(text):
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1)
               for c in text)


def fit(text, size):
    text = safe_text(text)
    if width(text) > size:
        result = ""
        for c in text:
            if width(result + c) > max(0, size - 1):
                break
            result += c
        text = result + ("~" if size else "")
    return text + " " * max(0, size - width(text))


def interval(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def frame(statuses, columns=100, lines=30, log_path=""):
    columns = max(1, columns)
    now = time.time()
    result = [fit("ssh-tunnel-watch  |  " + time.strftime("%H:%M:%S") + "  |  Ctrl+C: stop", columns).rstrip()]
    # Switch to stacked rows on small terminals; every line respects terminal width.
    if columns >= 64:
        fields = ["HOST", "STATE", "FAIL", "RETRY", "UP", "CHECK"]
        rows = [[s.name, s.phase, str(s.failures), interval(s.retry_at - now) if s.retry_at else "-",
                 interval(now - s.connected_at) if s.connected_at else "-", s.health] for s in statuses]
        sizes = [max([width(fields[i])] + [width(row[i]) for row in rows]) for i in range(6)]
        limits = [24, 12, 6, 8, 8, 7]
        sizes = [min(a, b) for a, b in zip(sizes, limits)]
        while sum(sizes) + 15 > columns:
            index = max(range(len(sizes)), key=lambda i: sizes[i])
            sizes[index] -= 1
        result.append(" | ".join(fit(f, size) for f, size in zip(fields, sizes)).rstrip())
        result.append("-+-".join("-" * size for size in sizes))
        result += [" | ".join(fit(f, size) for f, size in zip(row, sizes)).rstrip() for row in rows]
    else:
        for s in statuses:
            result.append(fit(f"{s.name}: {s.phase}  fail={s.failures}", columns).rstrip())
            result.append(fit(f"  retry={interval(s.retry_at - now) if s.retry_at else '-'} check={s.health}", columns).rstrip())
    for s in statuses:
        if s.forwards:
            result.append(fit(f"{s.name}: " + "; ".join(s.forwards), columns).rstrip())
        if s.error:
            result.append(fit(f"{s.name}: {s.error}", columns).rstrip())
        if s.last_notice:
            result.append(fit(f"{s.name} notice: {s.last_notice}", columns).rstrip())
    if log_path:
        result.append(fit("log: " + str(log_path), columns).rstrip())
    if not statuses:
        result.append(fit("No saved tunnel status.", columns).rstrip())
    available = max(1, lines - 1)  # Avoid writing the bottom-right cell and scrolling.
    if len(result) > available:
        result = result[:max(0, available - 1)] + [fit("... resize terminal or use status --json", columns).rstrip()]
    return "\n".join(result)


class Display:
    def __init__(self, stream=None, enabled=True, log_path=""):
        self.stream = stream or sys.stdout
        self.enabled = enabled and self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.log_path = log_path
        self.previous = None

    def __enter__(self):
        if self.enabled:
            self.stream.write("\x1b[?1049h\x1b[?25l")
            self.stream.flush()
        return self

    def update(self, statuses):
        if self.enabled:
            terminal = shutil.get_terminal_size((100, 30))
            content = frame(statuses, terminal.columns, terminal.lines, self.log_path)
            self.stream.write("\x1b[H\x1b[J" + content.replace("\n", "\r\n"))
            self.stream.flush()
        else:
            # State transitions only: no half-second heartbeat spam in journal/files.
            signature = [(s.name, s.phase, s.failures, s.error, s.last_notice) for s in statuses]
            if signature != self.previous:
                for s in statuses:
                    self.stream.write(f"{time.strftime('%H:%M:%S')} {safe_text(s.name)} {s.phase} "
                                      f"failures={s.failures} retry={interval(s.retry_at - time.time()) if s.retry_at else '-'} "
                                      f"{safe_text(s.error)}\n")
                self.stream.flush()
                self.previous = signature

    def __exit__(self, *args):
        if self.enabled:
            self.stream.write("\x1b[?25h\x1b[?1049l")
            self.stream.flush()
