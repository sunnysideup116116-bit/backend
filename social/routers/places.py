"""Server-owned Google Places helpers for frontend search surfaces."""

from fastapi import APIRouter, HTTPException

from models import PlacesAutocompleteRequest, PlacesDetailsRequest
from services.ayue_agent.google_places_client import (
    GooglePlacesError,
    autocomplete_places,
    place_details,
)


router = APIRouter(prefix="/api/places", tags=["Places"])


@router.post("/autocomplete")
def places_autocomplete(req: PlacesAutocompleteRequest):
    try:
        return {
            "suggestions": autocomplete_places(
                req.input,
                session_token=req.session_token,
            ),
        }
    except GooglePlacesError as exc:
        status = 503 if exc.code in {
            "google_places_autocomplete_disabled",
            "google_places_timeout",
            "google_places_unavailable",
            "google_places_rate_limited",
        } else 502
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.post("/details")
def places_details(req: PlacesDetailsRequest):
    try:
        return {
            "place": place_details(
                req.place_id,
                session_token=req.session_token,
            ),
        }
    except GooglePlacesError as exc:
        status = 503 if exc.code in {
            "google_places_autocomplete_disabled",
            "google_places_details_input_invalid",
            "google_places_timeout",
            "google_places_unavailable",
            "google_places_rate_limited",
        } else 502
        raise HTTPException(status_code=status, detail=exc.code) from exc
