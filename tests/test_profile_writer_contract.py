"""No runtime profile creator may bypass the shared uniqueness boundary."""
import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_27_runtime_creation_sites_share_boundary():
    wrappers, bypasses = [], []
    for path in (ROOT / "social").rglob("*.py"):
        if "tests" in path.parts: continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call): continue
            if isinstance(node.func, ast.Name) and node.func.id in {"write_profile", "ensure_profile"}:
                wrappers.append((str(path.relative_to(ROOT)), node.func.id))
            if not isinstance(node.func, ast.Attribute) or ast.unparse(node.func.value) != "profiles_coll": continue
            if node.func.attr in {"insert_one", "insert_many", "bulk_write", "replace_one"}:
                # Existing explicit destructive demo fixture owner, not an online creator.
                if path.name != "system.py" or node.func.attr != "insert_many": bypasses.append((str(path), node.lineno))
            if any(k.arg == "upsert" and not (isinstance(k.value, ast.Constant) and k.value.value is False) for k in node.keywords):
                bypasses.append((str(path), node.lineno))
    assert bypasses == []
    assert len(wrappers) == 27
    assert Counter(name for _, name in wrappers) == {"write_profile": 25, "ensure_profile": 2}


def test_helper_does_not_import_runtime_databases_or_manage_schema():
    path = ROOT / "social/services/profile_writer.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom): assert node.module not in {"database", "config", "neo4j"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"create_index", "drop_index", "delete_many", "delete_one", "replace_one", "insert_many"}
