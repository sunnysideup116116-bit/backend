import ast
from pathlib import Path


AGENT_API = (
    Path(__file__).resolve().parents[1] / "matchmaker_agent" / "agent_api.py"
)


def load_sanitize_function():
    source = AGENT_API.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "sanitize_chat_triples"
        ),
        None,
    )
    assert function is not None, "chat triple sanitization has not been integrated"
    module = ast.Module(
        body=[
            ast.Assign(
                targets=[ast.Name(id="ALLOWED_CHAT_TRIPLE_PREDICATES", ctx=ast.Store())],
                value=ast.Set(
                    elts=[
                        ast.Constant("LIKES"),
                        ast.Constant("WANTS"),
                        ast.Constant("MENTIONED"),
                    ]
                ),
            ),
            function,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(AGENT_API), "exec"), namespace)
    return namespace["sanitize_chat_triples"]


def load_graph_entity_key_function():
    source = AGENT_API.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "graph_entity_key"
        ),
        None,
    )
    assert function is not None, "chat graph entities are not session-scoped"
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(AGENT_API), "exec"), namespace)
    return namespace["graph_entity_key"]


def test_chat_triples_are_limited_and_predicates_are_allowlisted():
    sanitize = load_sanitize_function()

    triples = sanitize(
        [
            {"subject": " A ", "predicate": "likes", "object": " B "},
            {"subject": "A", "predicate": "DELETE", "object": "B"},
            {"subject": "", "predicate": "WANTS", "object": "B"},
        ]
    )

    assert triples == [{"subject": "A", "predicate": "LIKES", "object": "B"}]


def test_chat_graph_entity_keys_are_scoped_to_session():
    graph_entity_key = load_graph_entity_key_function()

    first = graph_entity_key("room-a", "電影")
    second = graph_entity_key("room-b", "電影")

    assert first != second
    assert first == graph_entity_key("room-a", " 電影 ")
