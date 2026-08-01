"""Emit a read-only inventory for the ``routers/chat.py`` split.

The script parses source files only. It never imports application modules,
connects to a database, or writes project files.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "routers" / "chat.py"
COLLECTIONS = {"profiles_coll", "messages_coll", "matches_coll"}
READ_METHODS = {"aggregate", "count_documents", "find", "find_one"}
WRITE_METHODS = {
    "delete_many", "delete_one", "find_one_and_delete", "find_one_and_replace",
    "find_one_and_update", "insert_many", "insert_one", "replace_one",
    "update_many", "update_one",
}
HTTP_METHODS = {"delete", "get", "patch", "post", "put"}


def _python_files() -> list[Path]:
    ignored = {"__pycache__", ".git", ".project-venv", ".local-venv", "venv"}
    return sorted(
        path for path in ROOT.rglob("*.py")
        if not any(part in ignored for part in path.parts)
    )


def _references(
    name: str,
    definition_line: int,
    sources: dict[Path, list[str]],
) -> dict[str, list[str]]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    result: dict[str, list[str]] = {"production": [], "tests": []}
    for path in sorted(sources):
        relative = path.relative_to(ROOT).as_posix()
        bucket = "tests" if relative.startswith("tests/") else "production"
        for line_number, line in enumerate(sources[path], 1):
            if path == TARGET and line_number == definition_line:
                continue
            if pattern.search(line):
                result[bucket].append(f"{relative}:{line_number}")
    return result


def _route(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str] | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        owner = decorator.func.value
        method = decorator.func.attr.lower()
        if not isinstance(owner, ast.Name) or owner.id != "router" or method not in HTTP_METHODS:
            continue
        if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
            continue
        return {"method": method.upper(), "path": "/api" + str(decorator.args[0].value)}
    return None


def _responsibility(path: str) -> str:
    if path.startswith("/api/direct_chat") or path.startswith("/api/messages") or path == "/api/contacts":
        return "public_messaging"
    if path.startswith("/api/mediator/private"):
        return "private_mediator"
    if path.startswith("/api/relationship"):
        return "relationship"
    if path == "/api/proactive_check":
        return "proactive_delivery"
    if path.startswith("/api/demo"):
        return "demo_maintenance"
    return "onboarding"


def _local_imports(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, object]]:
    imports: list[dict[str, object]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            imports.append({
                "line": child.lineno,
                "module": child.module,
                "names": [item.name for item in child.names],
            })
        elif isinstance(child, ast.Import):
            imports.append({
                "line": child.lineno,
                "module": None,
                "names": [item.name for item in child.names],
            })
    return sorted(imports, key=lambda item: int(item["line"]))


def _collection_operations(
    tree: ast.Module,
    owner_by_line: dict[int, str],
) -> list[dict[str, object]]:
    operations: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in COLLECTIONS:
            continue
        method = node.func.attr
        if method not in READ_METHODS | WRITE_METHODS:
            continue
        operations.append({
            "line": node.lineno,
            "owner": owner_by_line.get(node.lineno, "module"),
            "collection": receiver.id,
            "operation": method,
            "risk": "write" if method in WRITE_METHODS else "read",
        })
    return sorted(operations, key=lambda item: int(item["line"]))


def build_inventory() -> dict[str, object]:
    files = _python_files()
    sources = {
        path: path.read_text(encoding="utf-8-sig").splitlines()
        for path in files
    }
    source = TARGET.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=TARGET.relative_to(ROOT).as_posix())
    symbols: list[dict[str, object]] = []
    endpoints: list[dict[str, object]] = []
    owner_by_line: dict[int, str] = {}

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_line = int(getattr(node, "end_lineno", node.lineno))
        for line in range(node.lineno, end_line + 1):
            owner_by_line[line] = node.name
        refs = _references(node.name, node.lineno, sources)
        item: dict[str, object] = {
            "name": node.name,
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "start_line": node.lineno,
            "end_line": end_line,
            "production_references": refs["production"],
            "test_references": refs["tests"],
        }
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            item["function_local_imports"] = _local_imports(node)
            route = _route(node)
            if route:
                endpoint = {
                    **route,
                    "handler": node.name,
                    "responsibility": _responsibility(route["path"]),
                    "start_line": node.lineno,
                    "end_line": end_line,
                }
                endpoints.append(endpoint)
                item["endpoint"] = {"method": route["method"], "path": route["path"]}
        symbols.append(item)

    operations = _collection_operations(tree, owner_by_line)
    return {
        "version": 2,
        "purpose": "chat_router_split_phase0",
        "target": TARGET.relative_to(ROOT).as_posix(),
        "line_count": len(source.splitlines()),
        "symbol_count": len(symbols),
        "endpoint_count": len(endpoints),
        "collection_operation_count": len(operations),
        "endpoints": sorted(endpoints, key=lambda item: (str(item["path"]), str(item["method"]))),
        "symbols": symbols,
        "collection_operations": operations,
    }


if __name__ == "__main__":
    print(json.dumps(build_inventory(), ensure_ascii=False, indent=2))
