"""
main.py — 莉莉絲 Agent v2.2 啟動入口

修復：
  - Bug 2: Telegram thread 崩潰靜默 → 加監控 + 自動重啟
  - Bug 3: long mode 超過 4096 字消失 → 修正 fallback 邏輯（在 telegram_bot.py）
  - Bug 5: 歷史記錄上限不一致 → 統一為 40 條

v2.2 新增：
  - 更新感知：啟動時寫入版本號 + changelog 到 Redis
  - 莉莉絲會知道自己被更新了什麼
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
# 更新感知：版本號 + Changelog
# ----------------------------------------------------------

CURRENT_VERSION = "2.2"

# ★ 每次更新前在這裡填寫這次改了什麼，用自然語言寫給莉莉絲看
# 寫完 push，她上線後就會知道
CHANGELOG = "加了時間查詢工具，現在知道今天幾號幾點了。修了幾個穩定性問題。persona 也調整了，說話方式改了一些。"

def _write_version_to_redis():
    """
    啟動時比對版本號。
    若版本有變，寫入 changelog，設定 just_updated flag（1小時有效）。
    """
    if redis_client is None:
        return
    try:
        last_version = redis_client.get("lilith:version")
        # Redis 回傳 bytes，需要 decode
        if isinstance(last_version, bytes):
            last_version = last_version.decode()

        if last_version != CURRENT_VERSION:
            redis_client.set("lilith:version", CURRENT_VERSION)
            redis_client.set("lilith:changelog", CHANGELOG)
            redis_client.set("lilith:just_updated", "1", ex=3600)  # 1 小時有效
            logger.info(f"📦 版本更新：{last_version} → {CURRENT_VERSION}")
        else:
            # 版本沒變，清掉 just_updated（避免重啟時重複觸發）
            redis_client.delete("lilith:just_updated")
            logger.info(f"📦 版本未變：{CURRENT_VERSION}")
    except Exception as e:
        logger.error(f"[version] 寫入失敗: {e}")


# ----------------------------------------------------------
# Bug 2 修復：Telegram thread 監控 + 自動重啟
# ----------------------------------------------------------
_tg_restart_count = 0
_MAX_RESTARTS     = 5

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
    logger.info("🚀 Lilith Agent v2.2 啟動中……")

    # 更新感知：寫入版本資訊
    _write_version_to_redis()

    # Telegram watchdog 在背景跑
    watchdog = threading.Thread(target=_telegram_watchdog, daemon=True, name="TelegramWatchdog")
    watchdog.start()
    logger.info("✅ Telegram Watchdog 已啟動")

    # Web UI 在主 process 跑（阻塞在這裡）
    logger.info("✅ Web UI 啟動於 http://0.0.0.0:8000")
    run_web()
