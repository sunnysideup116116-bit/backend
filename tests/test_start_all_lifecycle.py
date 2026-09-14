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
        (self.root / "start_all.sh").write_bytes((ROOT / "start_all.sh").read_bytes())
        (self.root / "bin").mkdir()
        (self.root / "scripts").mkdir()
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
def stop(*_):
    time.sleep(0.15)
    (root/('stopped-'+port)).write_text('graceful')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
(root/('ready-'+port)).touch()
while True: time.sleep(0.05)
""")
        for name, port in (("guardrail", "8081"), ("risk", "8001"), ("matchmaker", "9001"), ("social", "8000")):
            self._executable(self.root / f"scripts/run_ayue_{name}.sh", f'#!/bin/sh\nexec "{sys.executable}" "{service}" {port}\n')
        self.env = dict(os.environ, STUB_ROOT=str(self.root), AYUE_SHUTDOWN_TIMEOUT_SECONDS="3")
        self.env["PATH"] = str(self.root / "bin") + os.pathsep + os.environ["PATH"]

    def _executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def _run(self, input_text="q\n", **env):
        return subprocess.run(["bash", str(self.root / "start_all.sh")], input=input_text,
                              text=True, capture_output=True, env={**self.env, **env}, timeout=12)

    def test_successful_quit_waits_for_every_service_to_finish_cleanup(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for port in ("8081", "8001", "9001", "8000"):
            self.assertEqual((self.root / f"stopped-{port}").read_text(), "graceful")
        self.assertNotIn("exceeded the shutdown", result.stdout)

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
