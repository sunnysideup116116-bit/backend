import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(tags=["Frontend"])
_IMAGE_DIR = Path(__file__).resolve().parents[1] / "images"
_AYUE_IMAGE_ASSETS = {
    "pet.gif": (_IMAGE_DIR / "pet.GIF", "image/gif"),
    "ayue-app-icon.png": (_IMAGE_DIR / "PNG-JPG" / "App Icon.PNG", "image/png"),
    "ayue-assessment.png": (_IMAGE_DIR / "PNG-JPG" / "皮康寫筆記.png", "image/png"),
    "ayue-match.png": (_IMAGE_DIR / "PNG-JPG" / "皮康愛心.png", "image/png"),
    "ayue-whisper.png": (_IMAGE_DIR / "PNG-JPG" / "講悄悄話.PNG", "image/png"),
}

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
    """Serve allowlisted Ayue artwork without exposing the asset directory."""
    asset = _AYUE_IMAGE_ASSETS.get(filename.lower())
    if asset is None:
        raise HTTPException(status_code=404, detail="Image not found")
    image_path, media_type = asset
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(image_path, media_type=media_type)
