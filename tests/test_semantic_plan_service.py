import importlib
import importlib.util


def load_service():
    module_name = "services.semantic_plan_service"
    assert importlib.util.find_spec(module_name) is not None, (
        "semantic plan service has not been integrated"
    )
    return importlib.import_module(module_name)


def test_format_chat_log_interpolates_real_message_values():
    service = load_service()

    chat_log = service.format_chat_log(
        [
            {"sender_id": "user_a", "content": "週六晚上去看電影？"},
            {"sender_id": "user_b", "content": "好，我七點後有空"},
        ]
    )

    assert "User user_a: 週六晚上去看電影？" in chat_log
    assert "User user_b: 好，我七點後有空" in chat_log
    assert "{message.get" not in chat_log
