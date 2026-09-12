from unittest.mock import patch

from fastapi import HTTPException

from models import ProfileUpdateRequest
from routers import system


def test_profile_update_mirrors_editable_fields_and_legacy_name_projection():
    request = ProfileUpdateRequest(
        user_id="owner",
        name="  新名字  ",
        phone=" 0912345678 ",
        age=28,
        region="高雄市",
        photo_id="photo-1",
        userinfo=" 喜歡散步 ",
    )

    with patch.object(system.profiles_coll, "update_one") as update:
        result = system.update_profile(request)

    assert result["status"] == "success"
    assert result["user_id"] == "owner"
    assert set(result["updated_fields"]) == {
        "age",
        "display_name",
        "name",
        "photo_id",
        "phone",
        "region",
        "userinfo",
    }
    assert update.call_args.args[0] == {"user_id": "owner"}
    assert update.call_args.args[1] == {
        "$set": {
            "name": "新名字",
            "display_name": "新名字",
            "phone": "0912345678",
            "age": 28,
            "region": "高雄市",
            "photo_id": "photo-1",
            "userinfo": "喜歡散步",
        },
    }
    assert update.call_args.kwargs == {"upsert": True}


def test_profile_update_maps_interest_to_matching_projection():
    request = ProfileUpdateRequest(user_id="owner", interest="  看展覽 ")

    with patch.object(system.profiles_coll, "update_one") as update:
        system.update_profile(request)

    assert update.call_args.args[1] == {
        "$set": {"interest": "看展覽", "initial_interest": "看展覽"},
    }


def test_profile_update_rejects_empty_name():
    request = ProfileUpdateRequest(user_id="owner", name=" ")

    with patch.object(system.profiles_coll, "update_one") as update:
        try:
            system.update_profile(request)
        except HTTPException as error:
            assert error.status_code == 422
        else:
            raise AssertionError("empty names must be rejected")

    update.assert_not_called()
