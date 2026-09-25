"""No network/provider: shallow history must not weaken frozen content checks."""
import hashlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from readiness_frozen_git import FROZEN_BLOBS, FrozenTestGit

ROOT = Path(__file__).resolve().parents[3]


class FrozenTestGitTests(unittest.TestCase):
    def setUp(self):
        self.reader = FrozenTestGit(ROOT)
        self.spec = next(iter(FROZEN_BLOBS))
        self.args = ["git", "show", self.spec]
        self.content = (ROOT / self.spec.split(":", 1)[1]).read_bytes()
        self.missing = subprocess.CalledProcessError(128, self.args)

    def test_pins_match_frozen_checked_in_content(self):
        for spec, digest in FROZEN_BLOBS.items():
            with self.subTest(spec=spec):
                self.assertEqual(hashlib.sha256((ROOT / spec.split(":", 1)[1]).read_bytes()).hexdigest(), digest)

    def test_full_history_is_preferred_and_checked(self):
        with patch("readiness_frozen_git.subprocess.check_output", return_value=self.content) as read:
            self.assertEqual(self.reader.check_output(self.args, cwd=ROOT), self.content)
            self.assertEqual(read.call_count, 1)

    def test_shallow_missing_commit_uses_only_pinned_bytes(self):
        with patch("readiness_frozen_git.subprocess.check_output", side_effect=[self.missing, b"true\n"]) as read, \
             patch("readiness_frozen_git.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            self.assertEqual(self.reader.check_output(self.args, cwd=ROOT), self.content)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(run.call_args.args[0][:3], ["git", "cat-file", "-e"])

    def test_non_shallow_missing_history_remains_failure(self):
        with patch("readiness_frozen_git.subprocess.check_output", side_effect=[self.missing, b"false\n"]):
            with self.assertRaises(subprocess.CalledProcessError):
                self.reader.check_output(self.args, cwd=ROOT)

    def test_shallow_present_commit_missing_blob_remains_failure(self):
        with patch("readiness_frozen_git.subprocess.check_output", side_effect=[self.missing, b"true\n"]), \
             patch("readiness_frozen_git.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(subprocess.CalledProcessError):
                self.reader.check_output(self.args, cwd=ROOT)

    def test_changed_git_blob_is_rejected(self):
        with patch("readiness_frozen_git.subprocess.check_output", return_value=b"changed"):
            with self.assertRaisesRegex(ValueError, "frozen_test_evidence_changed"):
                self.reader.check_output(self.args, cwd=ROOT)

    def test_changed_checkout_in_shallow_repo_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / self.spec.split(":", 1)[1]
            target.parent.mkdir(parents=True)
            target.write_bytes(b"changed gate")
            with patch("readiness_frozen_git.subprocess.check_output", side_effect=[self.missing, b"true\n"]), \
                 patch("readiness_frozen_git.subprocess.run", return_value=SimpleNamespace(returncode=1)):
                with self.assertRaisesRegex(ValueError, "frozen_test_evidence_changed"):
                    FrozenTestGit(root).check_output(self.args, cwd=root)

    def test_unknown_spec_is_never_salvaged(self):
        with patch("readiness_frozen_git.subprocess.check_output", side_effect=self.missing) as read:
            with self.assertRaises(subprocess.CalledProcessError):
                self.reader.check_output(["git", "show", "unknown:file"], cwd=ROOT)
            self.assertEqual(read.call_count, 1)

    def test_unrelated_git_command_is_unchanged(self):
        with patch("readiness_frozen_git.subprocess.check_output", return_value=b"head") as read:
            self.assertEqual(self.reader.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT), b"head")
            read.assert_called_once_with(["git", "rev-parse", "HEAD"], cwd=ROOT)

    def test_real_runner_does_not_import_test_adapter(self):
        for name in ("r33_data.py", "r34_final_contract.py", "run_r33_offline.py", "run_r34_final.py"):
            self.assertNotIn("readiness_frozen_git", (Path(__file__).parent / name).read_text())


if __name__ == "__main__":
    unittest.main()
