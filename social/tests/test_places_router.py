from unittest.mock import patch

from models import PlacesAutocompleteRequest, PlacesDetailsRequest
from routers.places import places_autocomplete, places_details


def test_places_autocomplete_returns_bounded_public_projection():
    with patch(
        "routers.places.autocomplete_places",
        return_value=[{
            "provider": "google",
            "place_id": "ChIJplace",
            "name": "台北車站",
            "address": "台北市中正區",
            "description": "台北車站，台北市中正區",
        }],
    ) as autocomplete:
        result = places_autocomplete(
            PlacesAutocompleteRequest(input="台北車", session_token="session-1"),
        )

    autocomplete.assert_called_once_with("台北車", session_token="session-1")
    assert result["suggestions"][0]["place_id"] == "ChIJplace"


def test_places_details_terminates_the_same_session():
    with patch(
        "routers.places.place_details",
        return_value={"place_id": "ChIJplace", "address": "台北市"},
    ) as details:
        result = places_details(
            PlacesDetailsRequest(
                place_id="ChIJplace",
                session_token="session-1",
            ),
        )

    details.assert_called_once_with("ChIJplace", session_token="session-1")
    assert result["place"]["address"] == "台北市"
