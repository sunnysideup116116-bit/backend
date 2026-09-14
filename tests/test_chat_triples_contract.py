import ast
from pathlib import Path


AGENT_API = (
    Path(__file__).resolve().parents[1]
    / "matchmaker_agent"
    / "agent_api.py"
)


def _find_receive_chat_triples(tree):
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "receive_chat_triples"
        ),
        None,
    )


def load_receive_chat_triples():
    source = AGENT_API.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    function = _find_receive_chat_triples(tree)
    assert function is not None, "chat triple ingestion has not been integrated"
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(AGENT_API), "exec"), namespace)
    return namespace["receive_chat_triples"]


def test_chat_triples_are_limited_and_predicates_are_allowlisted():
    source = AGENT_API.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    function = _find_receive_chat_triples(tree)
    allowed = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Set)
    )
    predicates = {ast.literal_eval(elt) for elt in allowed.elts}

    assert {"LIKES", "WANTS", "MENTIONED"} <= predicates
    assert "DELETE" not in predicates
    assert "IS_A" in predicates


def test_chat_triples_are_capped_at_sixteen():
    source = AGENT_API.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    function = _find_receive_chat_triples(tree)
    source_text = ast.get_source_segment(source, function)
    assert "[:16]" in source_text
