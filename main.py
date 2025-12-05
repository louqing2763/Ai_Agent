# ==========================================================
#   Congyin V7.5 — Telegram AI Companion (Continuity Mode)
# ==========================================================

import os
import io
import base64
import asyncio
import random
import time

from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes
)

# ---- core modules -------------------------------------------------
from core.persona import get_base_persona
from core.llm import call_openai, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp
from core.aer import regulate


# ------------------------ ENV --------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY      = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID     = os.getenv("ELEVENLABS_VOICE_ID")

ADMIN_ID = int(os.getenv("ADMIN_ID"))

REDIS_URL      = os.getenv("REDIS_URL")
REDISHOST      = os.getenv("REDISHOST")
REDISPORT      = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD  = os.getenv("REDISPASSWORD")


# --------------------------------------------------------------
# Redis Init
# --------------------------------------------------------------

redis_client = init_redis(
    REDIS_URL,
    REDISHOST,
    REDISPORT,
    REDISPASSWORD
)


# --------------------------------------------------------------
# Split 中文 / 日文
# --------------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# --------------------------------------------------------------
# 啟動問候（依照上次情緒自然延續）
# --------------------------------------------------------------

async def startup_greeting(app, chat_id, redis_client):
    state = load_state(chat_id, redis_client)

    last_emotion = state.get("emotion", "neutral")
    affinity = state.get("affinity", 1.0)

    prompt = f"""
    妳是佐奈聰音，剛啟動完成。
    請根據「上次情緒」自然接續心情。

    上次情緒：{last_emotion}
    親密度：{affinity}

    規則：
    - 不要提到系統、重啟、更新、機器人
    - 像上一秒的情緒被延續
    - 用一段柔軟、自然、有動作的問候
    - 只能使用中日雙語格式：中文|||日文
    """

    messages = [{"role": "system", "content": prompt}]
    out = await call_openai(messages)
    out = enforce_format(out)

    cn, jp = split_reply(out)

    await app.bot.send_message(chat_id, cn)

    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await app.bot.send_voice(chat_id, audio)

    # 標記啟動時間，避免啟動後立即推播
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)


# --------------------------------------------------------------
# 主回覆流程
# --------------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 更新說話時間（避免推播打擾）
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # AER：更新情緒數值
    aer = regulate(user_text, state)
    state["aer"] = aer
    save_state(chat_id, state, redis_client)

    # 打字動畫
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # 圖片分析
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)

        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 語音：目前不轉文字
    if voice_data:
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        user_text = "(語音轉文字未啟用)"

    # 是否需要搜索
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # 人設 + AER
    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = aer
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    out = await call_openai(messages)
    out = enforce_format(out)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)

    return out


# --------------------------------------------------------------
# Telegram Handlers
# --------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    text = update.message.text

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# --------------------------------------------------------------
# 推播（避免打擾聊天）
# --------------------------------------------------------------

async def active_push(context):
    chat_id = ADMIN_ID

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    now = time.time()

    # ⭐ 3 分鐘內使用者講過話 → 不推播
    if now - state.get("last_user_timestamp", 0) < 180:
        return

    # 隨機推播事件
    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(探頭) 我剛看到一個小消息：\n{news}"
    elif r < 0.66:
        content = "(輕輕靠過來) 你現在在做什麼？我…有一點想你了。"
    else:
        content = "(小心翼翼抓住你的袖子) 可以說一句話給我嗎？我想聽。"

    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = state.get("aer", {"emotion":"neutral","gesture":2,"affinity":1.0,"length":"normal"})
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_openai(messages)
    out = enforce_format(out)

    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)

    # 保存歷史
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)


# --------------------------------------------------------------
# Bot 啟動（post_init）
# --------------------------------------------------------------

async def on_startup(app):
    await startup_greeting(app, ADMIN_ID, redis_client)


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    # 推播（每 30 分鐘）
    app.job_queue.run_repeating(active_push, interval=1800, first=10)

    # 啟動問候
    app.post_init.append(on_startup)

    print("🚀 Congyin V7.5 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
