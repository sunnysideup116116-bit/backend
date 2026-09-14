import os
import sys
import warnings

# 1. 在載入任何模組前，強制屏蔽所有警告與環境變數
warnings.simplefilter('ignore')
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

# 屏蔽 Appwrite SDK 的特定警告
if not sys.warnoptions:
    warnings.simplefilter("ignore")

import uvicorn
from app.main import app

if __name__ == "__main__":
    port = int(os.environ.get("RISK_PORT", "8001"))
    host = os.environ.get("RISK_HOST", "127.0.0.1")
    uvicorn.run(
        "app.main:app", 
        host=host, 
        port=port, 
        reload=True,
        log_level="critical" # 降低 Uvicorn 本身的日誌噪音
    )

