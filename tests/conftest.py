import sys
from pathlib import Path


MAIN_APP_ROOT = Path(__file__).resolve().parents[1] / "main_app"
if str(MAIN_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(MAIN_APP_ROOT))
