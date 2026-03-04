"""
main.py — 莉莉絲 Agent v2.0 啟動入口

啟動兩個服務：
  - Telegram Bot（背景 thread，有自己的 event loop）
  - FastAPI Web UI（主 process，port 8000）
"""

import os
import time
import asyncio
import threading
import logging

os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset()
except AttributeError:
    pass

logging.basicConfig(
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# ----------------------------------------------------------
# 環境變數
# ----------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # Railway 直接用環境變數，不需要 .env 檔

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID           = int(os.getenv("ADMIN_ID", "0"))
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY")

if not TELEGRAM_BOT_TOKEN or not ADMIN_ID or not DEEPSEEK_API_KEY:
    print("❌ 缺少必要環境變數：TELEGRAM_BOT_TOKEN / ADMIN_ID / DEEPSEEK_API_KEY")
    exit(1)

# ----------------------------------------------------------
# Redis 初始化（兩個服務共用）
# ----------------------------------------------------------
from core.redis_store import init_redis

redis_client = init_redis(
    os.getenv("REDIS_URL"),
    os.getenv("REDISHOST"),
    int(os.getenv("REDISPORT", "6379")),
    os.getenv("REDISPASSWORD"),
)

# ----------------------------------------------------------
# 啟動函式
# ----------------------------------------------------------
def run_telegram():
    """Telegram Bot 在獨立 thread 中跑，有自己的 event loop"""
    from interfaces.telegram_bot import start_telegram
    asyncio.run(start_telegram(
        token        = TELEGRAM_BOT_TOKEN,
        admin_id     = ADMIN_ID,
        redis_client = redis_client,
        deepseek_key = DEEPSEEK_API_KEY,
    ))


def run_web():
    """FastAPI 在主 process 跑"""
    import uvicorn
    from interfaces.web_ui import create_app

    app = create_app(
        admin_id     = ADMIN_ID,
        redis_client = redis_client,
        deepseek_key = DEEPSEEK_API_KEY,
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


# ----------------------------------------------------------
# 入口
# ----------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Lilith Agent v2.0 啟動中……")

    # Telegram 在背景跑
    tg = threading.Thread(target=run_telegram, daemon=True)
    tg.start()
    print("✅ Telegram Bot 已啟動")

    # Web UI 在主 process 跑（阻塞在這裡）
    print("✅ Web UI 啟動於 http://0.0.0.0:8000")
    run_web()
