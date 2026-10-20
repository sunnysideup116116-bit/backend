"""Execute the canonical script with isolated fake services, never real ports."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartAllLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self._write_launcher()
        (self.root / "bin").mkdir()
        (self.root / "scripts").mkdir()
        # Satisfy the new pre-port dependency preflight without importing any
        # real runtime. This test still exercises only isolated fake services.
        for service_name in ("social", "matchmaker"):
            fake_python = self.root / f".local-venv/{service_name}/bin/python"
            fake_python.parent.mkdir(parents=True)
            self._executable(fake_python, "#!/bin/sh\nexit 0\n")
        pi_package = self.root / "pi_agent/node_modules/@earendil-works/pi-agent-core/package.json"
        pi_package.parent.mkdir(parents=True)
        pi_package.write_text('{}')
        (self.root / "pi_agent/bridge.mjs").write_text('process.exit(0);')
        self._executable(self.root / "bin/lsof", "#!/bin/sh\nexit 0\n")
        self._executable(self.root / "bin/sleep", f"#!{sys.executable}\nimport sys,time\ntime.sleep(min(float(sys.argv[1]), 0.02))\n")
        self._executable(self.root / "bin/curl", f"#!{sys.executable}\n" + """
import os,pathlib,sys
root=pathlib.Path(os.environ['STUB_ROOT'])
url=sys.argv[-1]
port=next(p for p in ('8000','8001','9001','8081') if ':'+p+'/' in url)
sys.exit(0 if (root/('ready-'+port)).exists() and os.environ.get('STUB_FAILED_PORT') != port else 22)
""")
        service = self.root / "service.py"
        service.write_text("""
import os,pathlib,signal,sys,time
root=pathlib.Path(os.environ['STUB_ROOT']);port=sys.argv[1]
(root/('pid-'+port)).write_text(str(os.getpid()))
with (root/('starts-'+port)).open('a') as starts: starts.write('started\\n')
lock=pathlib.Path(os.environ['STUB_START_ALL_LOCK_FILE']).resolve()
lock_inherited=False
for fd in os.listdir('/proc/self/fd'):
    try:
        if pathlib.Path(os.readlink('/proc/self/fd/'+fd)).resolve()==lock: lock_inherited=True
    except OSError: pass
(root/('lock-inherited-'+port)).write_text(str(lock_inherited))
def stop(*_):
    time.sleep(0.15)
    (root/('stopped-'+port)).write_text('graceful')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
(root/('ready-'+port)).touch()
if os.environ.get('STUB_EXIT_AFTER_PORT') == port:
    time.sleep(float(os.environ.get('STUB_EXIT_AFTER_SECONDS', '0.2')))
    raise SystemExit(int(os.environ.get('STUB_EXIT_STATUS', '70')))
while True: time.sleep(0.05)
""")
        for name, port in (("guardrail", "8081"), ("risk", "8001"), ("matchmaker", "9001"), ("social", "8000")):
            self._executable(self.root / f"scripts/run_ayue_{name}.sh", f'#!/bin/sh\nexec "{sys.executable}" "{service}" {port}\n')
        self.env = dict(
            os.environ,
            STUB_ROOT=str(self.root),
            STUB_START_ALL_LOCK_FILE=str(self.root / "launcher.lock"),
            AYUE_SHUTDOWN_TIMEOUT_SECONDS="3",
        )
        self.env["PATH"] = str(self.root / "bin") + os.pathsep + os.environ["PATH"]

    def _write_launcher(self):
        source = (ROOT / "start_all.sh").read_text(encoding="utf-8")
        production_lock = 'START_ALL_LOCK_FILE="/run/user/$(id -u)/ayue-backend-start-all.lock"'
        self.assertIn(production_lock, source)
        source = source.replace(
            production_lock,
            'START_ALL_LOCK_FILE="${STUB_START_ALL_LOCK_FILE:?}"',
        )
        (self.root / "start_all.sh").write_text(source, encoding="utf-8")

    def _executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def _run(self, input_text="q\n", **env):
        return subprocess.run(["bash", str(self.root / "start_all.sh")], input=input_text,
                              text=True, capture_output=True, env={**self.env, **env}, timeout=12)

    def _wait_for_social(self, process):
        deadline = time.monotonic() + 6
        while not (self.root / "ready-8000").exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.02)
        self.assertTrue((self.root / "ready-8000").exists())

    @staticmethod
    def _live_process_state(pid):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return None
        return stat.rsplit(")", 1)[1].split()[0]

    def test_successful_quit_waits_for_every_service_to_finish_cleanup(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for port in ("8081", "8001", "9001", "8000"):
            self.assertEqual((self.root / f"stopped-{port}").read_text(), "graceful")
            self.assertEqual((self.root / f"lock-inherited-{port}").read_text(), "False")
        self.assertNotIn("exceeded the shutdown", result.stdout)

    def test_lock_releases_after_normal_launcher_shutdown(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_manual_launch_is_rejected_while_systemd_style_launcher_is_active(self):
        process = subprocess.Popen(
            ["bash", str(self.root / "start_all.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            start_new_session=True,
        )
        try:
            self._wait_for_social(process)
            original_pids = {
                port: (self.root / f"pid-{port}").read_text()
                for port in ("8081", "8001", "9001", "8000")
            }
            duplicate = self._run()
            self.assertEqual(duplicate.returncode, 75, duplicate.stdout + duplicate.stderr)
            self.assertIn("already active", duplicate.stderr)
            self.assertIsNone(process.poll())
            for port, pid in original_pids.items():
                self.assertEqual((self.root / f"starts-{port}").read_text().count("started"), 1)
                self.assertEqual((self.root / f"pid-{port}").read_text(), pid)
                self.assertFalse((self.root / f"stopped-{port}").exists())
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 143, stdout + stderr)

    def test_two_launchers_racing_for_start_have_one_owner(self):
        launchers = [
            subprocess.Popen(
                ["bash", str(self.root / "start_all.sh")],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                start_new_session=True,
            )
            for _ in range(2)
        ]
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                rejected = [p for p in launchers if p.poll() == 75]
                active = [p for p in launchers if p.poll() is None]
                if rejected and active and (self.root / "ready-8000").exists():
                    break
                time.sleep(0.02)
            self.assertEqual(sum(p.poll() == 75 for p in launchers), 1)
            active = [p for p in launchers if p.poll() is None]
            self.assertEqual(len(active), 1)
            for port in ("8081", "8001", "9001", "8000"):
                self.assertEqual((self.root / f"starts-{port}").read_text().count("started"), 1)
                self.assertEqual((self.root / f"lock-inherited-{port}").read_text(), "False")
        finally:
            for process in launchers:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
            for process in launchers:
                if process.poll() is None:
                    process.communicate(timeout=5)

    def test_lock_releases_after_launcher_process_group_dies_without_orphans(self):
        process = subprocess.Popen(
            ["bash", str(self.root / "start_all.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
            start_new_session=True,
        )
        self._wait_for_social(process)
        service_pids = [
            int((self.root / f"pid-{port}").read_text())
            for port in ("8081", "8001", "9001", "8000")
        ]
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGKILL)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            live = [pid for pid in service_pids if self._live_process_state(pid) not in (None, "Z")]
            if not live:
                break
            time.sleep(0.02)
        self.assertEqual(
            [pid for pid in service_pids if self._live_process_state(pid) not in (None, "Z")],
            [],
        )

        restarted = self._run()
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)

    def test_social_process_failure_propagates_nonzero_for_systemd_restart(self):
        process = subprocess.Popen(
            ["bash", str(self.root / "start_all.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **self.env,
                "STUB_EXIT_AFTER_PORT": "8000",
                "STUB_EXIT_AFTER_SECONDS": "3",
                "STUB_EXIT_STATUS": "70",
            },
        )
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, stdout + stderr)
        self.assertIn("exited unexpectedly", stdout)
        for port in ("8081", "8001", "9001"):
            self.assertTrue((self.root / f"stopped-{port}").exists())

    def test_startup_failure_preserves_nonzero_status_and_cleans_started_services(self):
        result = self._run(STUB_FAILED_PORT="8001")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue((self.root / "stopped-8081").exists())
        self.assertTrue((self.root / "stopped-8001").exists())
        self.assertFalse((self.root / "pid-9001").exists())
        self.assertNotIn("RUNNING & HEALTHY", result.stdout)

    def test_guardrail_failure_is_not_reported_as_a_healthy_stack(self):
        result = self._run(STUB_FAILED_PORT="8081")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.root / "pid-8001").exists())
        self.assertNotIn("RUNNING & HEALTHY", result.stdout)

    def test_unknown_rollout_mode_stops_before_any_service_or_port_work(self):
        result = self._run(MATCH_RELATED_INTEREST_ROLLOUT_MODE='all')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any(self.root.glob('pid-*')))
        self.assertFalse((self.root / '.runtime-logs').exists())

    def test_enabled_accounts_mode_uses_canonical_launcher_without_a_list(self):
        result = self._run(MATCH_RELATED_INTEREST_ROLLOUT_MODE='enabled_accounts', MATCH_RELATED_INTEREST_CANARY_USER_IDS='')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for port in ('8081', '8001', '9001', '8000'):
            self.assertTrue((self.root / f'stopped-{port}').exists())

    def test_closed_stdin_keeps_services_running_until_sigterm_and_cleans_up(self):
        process = subprocess.Popen(["bash", str(self.root / "start_all.sh")], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            deadline = time.monotonic() + 6
            while not (self.root / "ready-8000").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "ready-8000").exists())
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            self.assertEqual(stdout.count("Standard input is closed"), 1)
            for port in ("8081", "8001", "9001", "8000"):
                self.assertTrue((self.root / f"stopped-{port}").exists())
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
