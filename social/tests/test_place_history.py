"""Only published place history may be used for Pi de-duplication."""

from unittest.mock import patch

from services.ayue_agent.shared.place_history import (
    clear_runtime_state,
    private_presented_place_identities,
    replace_presented_candidates,
)


def test_published_place_identities_are_owner_and_room_scoped():
    clear_runtime_state()
    cards = [{
        "name": "海風咖啡", "category": "cafe", "address_summary": "高雄市",
        "provider": "google", "place_id": "ChIJ-published",
        "map_url": "https://www.google.com/maps/place/published",
    }]
    with patch("services.ayue_agent.shared.place_history._collection", return_value=None):
        replace_presented_candidates("owner", "room", cards, origin_run_id="run")
        assert private_presented_place_identities(
            "owner", "room", categories=["cafe"],
        ) == {("google", "ChIJ-published")}
        assert private_presented_place_identities("other", "room", categories=["cafe"]) == set()
