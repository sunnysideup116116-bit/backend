"""The legacy Appwrite setup script must not embed an API key."""

import ast
from pathlib import Path


def test_setup_appwrite_api_key_comes_from_environment():
    script_path = Path(__file__).parents[1] / "db_setup" / "setup_appwrite.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"))

    assignments = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "API_KEY"
            for target in node.targets
        )
    ]

    assert len(assignments) == 1
    assert "APPWRITE_API_KEY" in ast.unparse(assignments[0])
    assert not isinstance(assignments[0], ast.Constant)
