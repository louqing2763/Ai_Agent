"""
tools/mood_tracker.py — 情緒狀態追蹤

每天固定時間由心跳排程呼叫：
  - 根據當天對話歷史推斷莉莉絲今天的情緒狀態
  - 結構：{ "label": "...", "description": "..." }
  - 存入 Redis: lilith:mood_today

凌晨由心跳排程呼叫：
  - 生成當天對話摘要，存入長期記憶
  - 清除短期記憶，準備新的一天
"""

import json
import asyncio
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


# ----------------------------------------------------------
# 😶 推斷今日情緒
# ----------------------------------------------------------
async def update_mood_today(redis_client, chat_id: int, deepseek_key: str):
    """
    讀取今天的對話歷史，呼叫 DeepSeek 推斷情緒，
    結果存入 Redis lilith:mood_today（TTL 36 小時）
    """
    from core.redis_store import load_history

    history = load_history(chat_id, redis_client)
    if not history:
        logger.info("[mood] 今天沒有對話記錄，跳過情緒更新。")
        return

    # 只取最近 20 條，避免 token 浪費
    recent = history[-20:]
    convo  = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Lilith'}: {m['content']}"
        for m in recent
        if m["role"] in ("user", "assistant")
    )

    prompt = (
        "以下是莉莉絲今天和 User 的對話片段。\n"
        "請根據這段對話，推斷莉莉絲今天整體的情緒狀態。\n\n"
        f"{convo}\n\n"
        "請只回傳 JSON，格式如下，不要有任何其他文字：\n"
        '{"label": "情緒標籤（如：平靜、雀躍、若有所思、有點落寞、溫柔、躁動）", '
        '"description": "一句話描述今天的狀態（從莉莉絲的第一人稱出發）"}'
    )

    payload = {
        "model":       "deepseek-chat",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens":  150,
    }

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {deepseek_key}",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, DEEPSEEK_URL,
            headers=headers, json=payload, timeout=30
        )
        if res.status_code != 200:
            logger.error(f"[mood] API 錯誤: {res.status_code}")
            return

        raw = res.json()["choices"][0]["message"]["content"].strip()

        # 清除可能的 markdown 包裝
        raw = raw.replace("```json", "").replace("```", "").strip()
        mood = json.loads(raw)

        # 驗證欄位
        if "label" not in mood or "description" not in mood:
            logger.error(f"[mood] 格式不符: {mood}")
            return

        redis_client.set(
            "lilith:mood_today",
            json.dumps(mood, ensure_ascii=False),
            ex=60 * 60 * 36  # TTL 36 小時，跨夜也保留
        )
        logger.info(f"[mood] 情緒更新完成: {mood['label']} — {mood['description']}")

    except json.JSONDecodeError as e:
        logger.error(f"[mood] JSON 解析失敗: {e} | raw: {raw}")
    except Exception as e:
        logger.error(f"[mood] 情緒更新失敗: {e}")


# ----------------------------------------------------------
# 📓 生成每日摘要
# ----------------------------------------------------------
async def generate_daily_summary(redis_client, chat_id: int, deepseek_key: str):
    """
    凌晨呼叫：
      1. 根據今天的對話生成摘要，存入長期記憶
      2. 清除短期記憶（history），準備新的一天
    """
    from core.redis_store import load_history, save_history
    from memory.long_term import save as mem_save

    history = load_history(chat_id, redis_client)
    if not history:
        logger.info("[daily] 今天沒有對話，跳過摘要生成。")
        return

    convo = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Lilith'}: {m['content']}"
        for m in history
        if m["role"] in ("user", "assistant")
    )

    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"以下是莉莉絲和 User 在 {today} 的完整對話。\n"
        "請用 2-3 句話，從莉莉絲的視角，摘要今天發生了什麼、聊了什麼、有什麼值得記住的事。\n"
        "語氣要像是莉莉絲自己在寫日記，不是客觀報告。\n\n"
        f"{convo}\n\n"
        "請直接給摘要文字，不要加任何標題或格式。"
    )

    payload = {
        "model":       "deepseek-chat",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens":  200,
    }

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {deepseek_key}",
    }

    try:
        res = await asyncio.to_thread(
            requests.post, DEEPSEEK_URL,
            headers=headers, json=payload, timeout=30
        )
        if res.status_code != 200:
            logger.error(f"[daily] API 錯誤: {res.status_code}")
            return

        summary = res.json()["choices"][0]["message"]["content"].strip()

        # 存入長期記憶
        entry = f"[{today} 日記] {summary}"
        await asyncio.to_thread(mem_save, redis_client, chat_id, "assistant", entry)
        logger.info(f"[daily] 摘要已存入長期記憶: {summary[:50]}...")

        # 清除短期記憶
        save_history(chat_id, [], redis_client)
        logger.info("[daily] 短期記憶已清除，準備新的一天。")

    except Exception as e:
        logger.error(f"[daily] 摘要生成失敗: {e}")
