from pathlib import Path
import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ssh_tunnel_watch.config import ConfigError, load_config
from ssh_tunnel_watch.state import Status, safe_text
from ssh_tunnel_watch.ui import frame, width


class ConfigTests(unittest.TestCase):
    def test_rejects_invalid_config_before_starting_processes(self):
        cases = [
            {"defaults": {"server_alive_count_max": 0}},
            {"defaults": {"backoff": {"factor": 0.5}}},
            {"defaults": {"backoff": {"jitter": 2}}},
            {"defaults": {"backoff": {"initial": float("nan")}}},
            {"notify": {"command": "sh -c unexpected"}},
            {"tunnels": [{"host": "-oProxyCommand=bad"}]},
            {"tunnels": [{"host": "D", "options": {"backoff": None}}]},
            {"tunnels": [{"host": "D"}, {"host": "D"}]},
            {"defaults": {"typo_timeout": 10}},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            for case in cases:
                with self.subTest(case=case):
                    path.write_text(json.dumps({"tunnels": [{"host": "D"}], **case}))
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_relative_ssh_config_and_partial_backoff_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"defaults": {"backoff": {"initial": 3}},
                                        "tunnels": [{"host": "D", "ssh_config": "ssh.conf",
                                                     "options": {"backoff": {"maximum": 20}}}]}))
            tunnel = load_config(path).tunnels[0]
            self.assertEqual(tunnel.ssh_config, str((path.parent / "ssh.conf").resolve()))
            self.assertEqual(tunnel.options.backoff.initial, 3)
            self.assertEqual(tunnel.options.backoff.maximum, 20)

    def test_dashboard_fits_narrow_wide_and_unicode_terminals(self):
        rows = [Status("D", "D", phase="CONNECTING", error="网络失败" * 100,
                       forwards=("remoteforward 11020 [localhost]:11020",)), Status("CPU", "CPU")]
        for columns in [1, 12, 40, 64, 80, 160]:
            for lines in [1, 4, 24]:
                with self.subTest(columns=columns, lines=lines):
                    output = frame(rows, columns, lines, "/a/long/path/" * 10)
                    self.assertLessEqual(len(output.splitlines()), max(1, lines - 1))
                    self.assertTrue(all(width(line) <= columns for line in output.splitlines()))
        self.assertNotIn("\x1b", safe_text("\x1b[2Jerror\x1b]2;malicious\x07"))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.fake = self.directory / "ssh"
        # Avoid relying on the checkout being executable, or on a shebang's PATH.
        source = (ROOT / "tests/fake_ssh.py").read_text()
        self.fake.write_text("#!" + sys.executable + "\n" + "\n".join(source.splitlines()[1:]))
        self.fake.chmod(0o755)
        self.environment = dict(os.environ, STW_FAKE_ROOT=str(self.directory))
        self.processes = []

    def tearDown(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        self.temporary.cleanup()

    def config(self, hosts, notify=None, options=None, health=False):
        defaults = {"check_interval": 0.1, "probe_timeout": 0.3, "probe_failures": 2,
                    "stable_after": 1, "startup_timeout": 1,
                    "backoff": {"initial": 0.15, "factor": 2, "maximum": 0.4, "jitter": 0}}
        defaults.update(options or {})
        result = {"ssh_binary": str(self.fake), "defaults": defaults,
                  "notify": notify or {},
                  "tunnels": [{"name": h, "host": h, "health_command": ["probe"] if health else []} for h in hosts]}
        path = self.directory / "config.json"
        path.write_text(json.dumps(result))
        return path

    def launch(self, path, duration=6):
        process = subprocess.Popen([sys.executable, str(ROOT / "bin/ssh-tunnel-watch"), "run",
                                    "--config", str(path), "--state-dir", str(self.directory / "state"),
                                    "--no-ui", "--duration", str(duration)], env=self.environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def until(self, predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.025)
        self.fail("Timed out waiting for expected lifecycle event")

    def state(self, host):
        return json.loads((self.directory / "state" / (host + ".json")).read_text())

    def events(self):
        return [json.loads(line) for line in (self.directory / "state/events.jsonl").read_text().splitlines()]

    def stop(self, process):
        process.send_signal(signal.SIGINT)
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)

    def test_independent_hosts_exponential_backoff_and_clean_shutdown(self):
        process = self.launch(self.config(["down", "up"]))
        self.until(lambda: self.state("down")["failures"] >= 4)
        self.assertEqual(self.state("up")["phase"], "SSH_READY")
        events = [e for e in self.events() if e["name"] == "down" and e["event"] == "disconnected"]
        self.assertEqual([e["retry_seconds"] for e in events[:4]], [0.15, 0.3, 0.4, 0.4])
        pid = self.state("up")["pid"]
        self.stop(process)
        self.assertEqual(self.state("up")["phase"], "STOPPED")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual((self.directory / "state/up.json").stat().st_mode & 0o777, 0o600)

    def test_flapping_counts_until_stable_and_alert_recovers(self):
        sink = self.directory / "alerts.jsonl"
        hook = self.directory / "notify.py"
        hook.write_text("import sys,pathlib; p=pathlib.Path(sys.argv[1]); "
                        "f=p.open('a');f.write(sys.stdin.read()+'\\n');f.close()")
        notify = {"after_failures": 2, "cooldown": 10, "timeout": 1,
                  "command": [sys.executable, str(hook), str(sink)]}
        process = self.launch(self.config(["flap"], notify))
        self.until(lambda: sink.exists() and "recovery" in sink.read_text())
        messages = [json.loads(line) for line in sink.read_text().splitlines()]
        self.assertEqual([m["event"] for m in messages], ["failure", "recovery"])
        self.assertEqual(messages[0]["failures"], "2")
        self.assertEqual(self.state("flap")["failures"], 0)
        self.stop(process)

    def test_failed_health_restarts_and_recovers_with_actual_processes(self):
        process = self.launch(self.config(["up"], health=True))
        self.until(lambda: self.state("up")["phase"] == "HEALTHY")
        first_pid = self.state("up")["pid"]
        (self.directory / "probe-fail").touch()
        self.until(lambda: self.state("up")["failures"] >= 1)
        (self.directory / "probe-fail").unlink()
        self.until(lambda: self.state("up")["phase"] == "HEALTHY" and self.state("up")["pid"] != first_pid)
        with self.assertRaises(ProcessLookupError):
            os.kill(first_pid, 0)
        self.stop(process)

    def test_hung_probe_is_killed_on_timeout_and_not_left_after_stop(self):
        (self.directory / "probe-hang").touch()
        process = self.launch(self.config(["up"], health=True))
        self.until(lambda: (self.directory / "probe-pid").exists())
        pid = int((self.directory / "probe-pid").read_text())
        self.until(lambda: self.state("up")["probe_failures"] >= 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.stop(process)

    def test_duplicate_supervisor_fails_without_disturbing_existing_tunnel(self):
        path = self.config(["up"])
        process = self.launch(path)
        self.until(lambda: self.state("up")["phase"] == "SSH_READY")
        pid = self.state("up")["pid"]
        duplicate = self.launch(path)
        _, error = duplicate.communicate(timeout=5)
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("already managed", error)
        os.kill(pid, 0)
        self.stop(process)

    def test_broken_notification_does_not_block_retries(self):
        notify = {"after_failures": 1, "cooldown": 10, "timeout": 0.15,
                  "command": [sys.executable, "-c", "import time;time.sleep(60)"]}
        process = self.launch(self.config(["down", "up"], notify))
        self.until(lambda: self.state("down")["failures"] >= 3)
        self.assertEqual(self.state("up")["phase"], "SSH_READY")
        self.assertTrue(any(e["event"] == "notification_failed" for e in self.events()))
        self.stop(process)

    def test_credential_arguments_are_not_logged_or_sent_in_notification_payload(self):
        # The injected hook receives a secret argument but events/status never do.
        sink = self.directory / "notice"
        script = self.directory / "notify.py"
        script.write_text("import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())")
        notify = {"after_failures": 1, "cooldown": 10, "timeout": 1,
                  "command": [sys.executable, str(script), str(sink), "SENTINEL_PRIVATE_KEY"]}
        process = self.launch(self.config(["down"], notify))
        self.until(lambda: sink.exists())
        self.assertNotIn("SENTINEL_PRIVATE_KEY", sink.read_text())
        self.assertNotIn("SENTINEL_PRIVATE_KEY", json.dumps(self.events()))
        self.assertNotIn("SENTINEL_PRIVATE_KEY", json.dumps(self.state("down")))
        self.stop(process)


class InstallTests(unittest.TestCase):
    def test_zipapp_install_runs_offline_and_preserves_existing_executable(self):
        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            command = [sys.executable, str(ROOT / "install.py"), "--prefix", str(prefix)]
            subprocess.run(command, check=True, capture_output=True)
            tool = prefix / "bin/ssh-tunnel-watch"
            original = tool.read_bytes()
            subprocess.run(command, check=True, capture_output=True)
            backups = list((prefix / "bin").glob("ssh-tunnel-watch.before-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            result = subprocess.run([sys.executable, str(tool), "--version"], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "0.1.0")


if __name__ == "__main__":
    unittest.main()
