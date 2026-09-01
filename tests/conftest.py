import sys
from pathlib import Path


SOCIAL_ROOT = Path(__file__).resolve().parents[1] / "social"
if str(SOCIAL_ROOT) not in sys.path:
    sys.path.insert(0, str(SOCIAL_ROOT))
