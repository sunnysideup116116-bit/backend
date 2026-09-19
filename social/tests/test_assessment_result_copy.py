import pytest
from services.assessment_session_service import _completion_reply
from services.ayue_agent.shared.confirmation import confirmation_display


@pytest.mark.parametrize('kind', ['big_five', 'deep_profile'])
def test_analysis_appears_once_and_button_card_has_no_reply_instructions(kind):
    text = _completion_reply(kind, {'summary': '喜歡交流，也享受獨處'})
    display = confirmation_display({'tool_name': 'profile.commit_assessment', 'preview_text': text})
    assert text.count('喜歡交流') == 1
    assert text.count('也享受獨處') == 1
    assert '回覆' not in text
    assert 'summary' not in display
    assert display['title'] == '套用探索結果'
    assert '回覆' not in str(display)
