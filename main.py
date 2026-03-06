"""
main.py — 莉莉絲 Agent v2.1 啟動入口

修復：
  - Bug 2: Telegram thread 崩潰靜默 → 加監控 + 自動重啟
  - Bug 3: long mode 超過 4096 字消失 → 修正 fallback 邏輯（在 telegram_bot.py）
  - Bug 5: 歷史記錄上限不一致 → 統一為 40 條
"""

import os
import sys
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

logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# 環境變數
# ----------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID           = int(os.getenv("ADMIN_ID", "0"))
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY")

if not TELEGRAM_BOT_TOKEN or not ADMIN_ID or not DEEPSEEK_API_KEY:
    logger.critical("❌ 缺少必要環境變數：TELEGRAM_BOT_TOKEN / ADMIN_ID / DEEPSEEK_API_KEY")
    sys.exit(1)

# ----------------------------------------------------------
# Redis 初始化
# ----------------------------------------------------------
from core.redis_store import init_redis

redis_client = init_redis(
    os.getenv("REDIS_URL"),
    os.getenv("REDISHOST"),
    int(os.getenv("REDISPORT", "6379")),
    os.getenv("REDISPASSWORD"),
)

# ----------------------------------------------------------
# Bug 2 修復：Telegram thread 監控 + 自動重啟
# ----------------------------------------------------------
_tg_restart_count = 0
_MAX_RESTARTS     = 5     # 最多重啟 5 次，避免無限循環

def run_telegram():
    """Telegram Bot 在獨立 thread 中跑，有自己的 event loop"""
    from interfaces.telegram_bot import start_telegram
    asyncio.run(start_telegram(
        token        = TELEGRAM_BOT_TOKEN,
        admin_id     = ADMIN_ID,
        redis_client = redis_client,
        deepseek_key = DEEPSEEK_API_KEY,
    ))

def _telegram_watchdog():
    """
    監控 Telegram thread，崩潰時自動重啟。
    最多重啟 _MAX_RESTARTS 次，超過後記錄 critical log。
    """
    global _tg_restart_count

    while True:
        _tg_restart_count += 1
        if _tg_restart_count > 1:
            logger.warning(f"⚠️  Telegram Bot 重啟中（第 {_tg_restart_count - 1} 次）……")

        tg = threading.Thread(target=run_telegram, daemon=True, name="TelegramBot")
        tg.start()

        # 等待 thread 結束（正常或異常）
        tg.join()

        if _tg_restart_count > _MAX_RESTARTS:
            logger.critical(
                f"💀 Telegram Bot 已崩潰 {_MAX_RESTARTS} 次，停止重啟。"
                f"請檢查 TELEGRAM_BOT_TOKEN 是否正確，或 Telegram API 是否可連線。"
            )
            break

        logger.error("❌ Telegram Bot thread 意外結束，5 秒後重啟……")
        time.sleep(5)

# ----------------------------------------------------------
# Web UI
# ----------------------------------------------------------
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
    logger.info("🚀 Lilith Agent v2.1 啟動中……")

    # Telegram watchdog 在背景跑
    watchdog = threading.Thread(target=_telegram_watchdog, daemon=True, name="TelegramWatchdog")
    watchdog.start()
    logger.info("✅ Telegram Watchdog 已啟動")

    # Web UI 在主 process 跑（阻塞在這裡）
    logger.info("✅ Web UI 啟動於 http://0.0.0.0:8000")
    run_web()
