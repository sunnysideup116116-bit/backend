from unittest.mock import Mock, patch

from fastapi import HTTPException
import pytest

from models import CalendarRescheduleRequest
from routers.calendar import reschedule_date_event


def test_reschedule_forwards_the_revision_that_the_client_confirmed():
    event = {"participants": ["owner", "other"], "revision": 9}
    request = CalendarRescheduleRequest(user_id="owner", date="2026-09-20", start_time="18:00", end_time="20:00", expected_revision=3)
    with patch("routers.calendar.calendar_events_coll", Mock(find_one=Mock(return_value=event))), patch(
        "services.date_coordination_service.request_reschedule",
        side_effect=HTTPException(status_code=409, detail="約會剛剛已變更"),
    ) as change:
        with pytest.raises(HTTPException) as raised:
            reschedule_date_event("event-1", request)
    assert raised.value.status_code == 409
    assert change.call_args.kwargs["expected_revision"] == 3
    assert "expected_revision" not in change.call_args.args[3]
