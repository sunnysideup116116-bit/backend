"""Test-only frozen-blob adapter for shallow CI checkouts; never fetches history.

Digests below were read from the original freeze commits, not regenerated from
the test checkout. Real experiment runners retain their original git-show guards.
"""
import hashlib
from pathlib import Path
import subprocess


FROZEN_BLOBS = {
    "3ee4d91395c2f4648f02bd107f016f33651bf819:tests/readiness/neo4j/r33_gate.json":
        "01c04e12d1345ae2dc70296a9aa25d244f87c68fa53ac6892b929888c7737990",
    "3ee4d91395c2f4648f02bd107f016f33651bf819:tests/readiness/neo4j/R33_ACCEPTANCE_GATE.md":
        "7493c6b76993282399d0e34412e591f242373b20a30d89370659dd87989647da",
    "132cba404b4ac8294308f22e93aa5573d54de23c:tests/readiness/neo4j/r34_selected_config.json":
        "6dd3806ee09f72eb4f9e0a3271fb2c0755a0d22e25a57af87dd1a6cfcb57421a",
}


class FrozenTestGit:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def check_output(self, args, **kwargs):
        known = (isinstance(args, (list, tuple)) and len(args) == 3
                 and list(args[:2]) == ["git", "show"]
                 and args[2] in FROZEN_BLOBS
                 and set(kwargs) == {"cwd"}
                 and Path(kwargs["cwd"]).resolve() == self.root)
        if not known:
            return subprocess.check_output(args, **kwargs)
        spec = args[2]
        try:
            content = subprocess.check_output(args, stderr=subprocess.DEVNULL, **kwargs)
        except subprocess.CalledProcessError:
            shallow = subprocess.check_output(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=self.root
            ).strip()
            if shallow != b"true":
                raise
            commit, relative_path = spec.split(":", 1)
            # A present commit with a broken/missing blob is not a shallow-history miss.
            present = subprocess.run(
                ["git", "cat-file", "-e", commit + "^{commit}"], cwd=self.root,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if present.returncode == 0:
                raise
            content = (self.root / relative_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != FROZEN_BLOBS[spec]:
            raise ValueError("frozen_test_evidence_changed")
        return content
