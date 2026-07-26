from copy import deepcopy


DATE_FORM_FIELDS = ("date", "time", "activity", "budget")


class StaleDateFormError(ValueError):
    pass


class InvalidDateParticipantError(ValueError):
    pass


def normalize_date_form(form: dict | None) -> dict[str, str]:
    source = form or {}
    return {
        field: str(source.get(field) or "").strip()[:200]
        for field in DATE_FORM_FIELDS
    }


def new_date_coordination(now: float) -> dict:
    return {
        "version": 2,
        "status": "gathering",
        "form": normalize_date_form({}),
        "form_revision": 1,
        "confirmations": {},
        "established_at": now,
    }


def update_date_form(state: dict, form: dict | None) -> dict:
    updated = deepcopy(state or {})
    updated["version"] = 2
    updated["form"] = normalize_date_form(form)
    updated["form_revision"] = max(int(updated.get("form_revision", 1)), 1) + 1
    updated["confirmations"] = {}
    if all(updated["form"].get(field) for field in ("date", "time", "activity")):
        updated["status"] = "active"
    else:
        updated["status"] = "gathering"
    updated.pop("completed_at", None)
    return updated


def confirm_date_form(
    state: dict,
    *,
    user_id: str,
    participant_ids: tuple[str, str],
    expected_revision: int,
    now: float,
) -> dict:
    updated = deepcopy(state or {})
    revision = int(updated.get("form_revision", 1))
    updated["version"] = 2
    updated["form_revision"] = revision
    updated["form"] = normalize_date_form(updated.get("form"))
    if expected_revision != revision:
        raise StaleDateFormError(
            f"date form revision {expected_revision} is stale; current revision is {revision}"
        )

    participants = tuple(dict.fromkeys(str(item) for item in participant_ids if item))
    if user_id not in participants or len(participants) != 2:
        raise InvalidDateParticipantError("confirmation user is not a match participant")

    confirmations = dict(updated.get("confirmations") or {})
    confirmations[user_id] = {"revision": revision, "confirmed_at": now}
    updated["confirmations"] = confirmations

    if all(
        (confirmations.get(participant) or {}).get("revision") == revision
        for participant in participants
    ):
        updated["status"] = "completed"
        updated["completed_at"] = now
    return updated
