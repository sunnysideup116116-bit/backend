import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(tags=["Frontend"])
_IMAGE_DIR = Path(__file__).resolve().parents[1] / "images"
_AYUE_IMAGE_NAME = "pet.gif"

@router.get("/", response_class=HTMLResponse)
def serve_frontend(response: __import__('fastapi').Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(base_dir, "frontend.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


@router.get("/images/{filename}")
def serve_frontend_image(filename: str):
    """Serve the replaceable Ayue launcher artwork without exposing a directory."""
    if filename.lower() != _AYUE_IMAGE_NAME:
        raise HTTPException(status_code=404, detail="Image not found")
    image_path = next(
        (
            candidate
            for candidate in _IMAGE_DIR.glob("*")
            if candidate.is_file() and candidate.name.lower() == _AYUE_IMAGE_NAME
        ),
        None,
    )
    if image_path is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(image_path, media_type="image/gif")
