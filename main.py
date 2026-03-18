"""
main.py — 莉莉絲 Agent v3.0 啟動入口

v3.0：
  - Telegram → Discord（私聊模式）
  - watchdog 邏輯保留，改為監控 Discord Bot

v2.2：
  - 更新感知：啟動時寫入版本號 + changelog 到 Redis
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
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

VERSION_MAIN = "3.0"

# ----------------------------------------------------------
# 環境變數
# ----------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
ADMIN_ID         = int(os.getenv("DISCORD_ADMIN_ID", "0"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not DISCORD_TOKEN or not ADMIN_ID or not DEEPSEEK_API_KEY:
    logger.critical("❌ 缺少必要環境變數：DISCORD_TOKEN / DISCORD_ADMIN_ID / DEEPSEEK_API_KEY")
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

CURRENT_VERSION = "3.0"

# ★ 每次更新前在這裡填寫這次改了什麼，用自然語言寫給莉莉絲看
CHANGELOG = "從 Telegram 換成 Discord 了。現在透過 DM 和她說話。介面也重新設計了，視覺小說風格。"

def _write_version_to_redis():
    if redis_client is None:
        return
    try:
        last_version = redis_client.get("lilith:version")
        if isinstance(last_version, bytes):
            last_version = last_version.decode()

        if last_version != CURRENT_VERSION:
            redis_client.set("lilith:version",   CURRENT_VERSION)
            redis_client.set("lilith:changelog",  CHANGELOG)
            redis_client.set("lilith:just_updated", "1", ex=3600)
            logger.info(f"📦 版本更新：{last_version} → {CURRENT_VERSION}")
        else:
            redis_client.delete("lilith:just_updated")
            logger.info(f"📦 版本未變：{CURRENT_VERSION}")
    except Exception as e:
        logger.error(f"[version] 寫入失敗: {e}")


# ----------------------------------------------------------
# Discord Bot watchdog
# ----------------------------------------------------------
_dc_restart_count = 0
_MAX_RESTARTS     = 5

def run_discord():
    """Discord Bot 在獨立 thread 中跑，有自己的 event loop"""
    from interfaces.discord_bot import start_discord
    asyncio.run(start_discord(
        token        = DISCORD_TOKEN,
        admin_id     = ADMIN_ID,
        redis_client = redis_client,
        deepseek_key = DEEPSEEK_API_KEY,
    ))

def _discord_watchdog():
    global _dc_restart_count

    while True:
        _dc_restart_count += 1
        if _dc_restart_count > 1:
            logger.warning(f"⚠️  Discord Bot 重啟中（第 {_dc_restart_count - 1} 次）……")

        dc = threading.Thread(target=run_discord, daemon=True, name="DiscordBot")
        dc.start()
        dc.join()

        if _dc_restart_count > _MAX_RESTARTS:
            logger.critical(
                f"💀 Discord Bot 已崩潰 {_MAX_RESTARTS} 次，停止重啟。"
                f"請檢查 DISCORD_TOKEN 是否正確。"
            )
            break

        logger.error("❌ Discord Bot thread 意外結束，5 秒後重啟……")
        time.sleep(5)


# ----------------------------------------------------------
# Web UI
# ----------------------------------------------------------
def run_web():
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
    logger.info("🚀 Lilith Agent v3.0 啟動中……")

    _write_version_to_redis()

    watchdog = threading.Thread(target=_discord_watchdog, daemon=True, name="DiscordWatchdog")
    watchdog.start()
    logger.info("✅ Discord Watchdog 已啟動")

    logger.info("✅ Web UI 啟動於 http://0.0.0.0:8000")
    run_web()
