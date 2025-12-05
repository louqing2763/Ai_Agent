# ==========================================================
#   Congyin V7.2 — Telegram AI Companion (Modular Version)
#   Author: 落卿
# ==========================================================

import os
import io
import base64
import asyncio
import random
import pytz

from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler,
filters, ContextTypes

# ---- modules -------------------------------------------------
from core.persona import get_base_persona
from core.llm import call_openai, call_deepseek, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp


# ------------------------ ENV --------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY    = os.getenv("DEEPSEEK_API_KEY")
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
# AER Default State
# --------------------------------------------------------------

def get_default_aer():
    return {
        "emotion": "neutral",   # low / neutral / high
        "gesture": 2,           # 1 ~ 3
        "affinity": 0.5,        # 0.0 ~ 1.0
        "length": "normal"      # short / normal / long
    }


# --------------------------------------------------------------
# Split 中文 / 日文
# --------------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# --------------------------------------------------------------
# 產生回覆（主流程）
# --------------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 若 state 沒有 AER → 初始化
    if "aer" not in state:
        state["aer"] = get_default_aer()

    aer = state["aer"]

    # 輸入中動畫
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # ------------------- 圖片模式 ---------------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)

        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # ------------------- 語音模式（暫時簡化） -------------------
    if voice_data:
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        user_text = "(語音轉文字尚未啟用)"

    # ------------------- 是否需要搜尋 -------------------
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # ------------------- 組裝人物設定 -------------------
    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = aer
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # ------------------- LLM Routing -------------------
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})
        out = await call_openai(messages)
    else:
        out = await call_deepseek(messages)

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
    state = load_state(chat_id, redis_client)

    # 若未初始化 AER → 初始化
    if "aer" not in state:
        state["aer"] = get_default_aer()

    # 語音模式開關
    if "開啟語音" in text:
        state["voice_mode"] = True
        save_state(chat_id, state, redis_client)
        await update.message.reply_text("(語音模式啟動)")
        return

    if "關閉語音" in text:
        state["voice_mode"] = False
        save_state(chat_id, state, redis_client)
        await update.message.reply_text("(語音模式關閉)")
        return

    # 一般回覆
    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


async def handle_photo(update: Update, context):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    if "aer" not in state:
        state["aer"] = get_default_aer()

    f = await update.message.photo[-1].get_file()
    data = await f.download_as_bytearray()
    b64 = base64.b64encode(data).decode()

    out = await generate_reply(chat_id, image_b64=b64)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


async def handle_voice(update: Update, context):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    if "aer" not in state:
        state["aer"] = get_default_aer()

    f = await update.message.voice.get_file()
    data = await f.download_as_bytearray()

    out = await generate_reply(chat_id, voice_data=data)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# --------------------------------------------------------------
# 推播（定時主動訊息）
# --------------------------------------------------------------

async def active_push(context):
    chat_id = ADMIN_ID

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 未初始化 AER → 初始化
    if "aer" not in state:
        state["aer"] = get_default_aer()

    aer = state["aer"]

    # 若 chatbot 在睡眠 → 不推播
    if state.get("sleeping"):
        return

    # 推播內容類型
    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(輕快跑來) 給你看個我剛看到的：\n{news}"

    elif r < 0.66:
        content = "(探頭) 你現在在做什麼？我有點想你。"

    else:
        content = "(靠近) 可以跟我說一句話嗎？我想聽。"

    # Persona
    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = aer
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_deepseek(messages)
    out = enforce_format(out)

    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)

    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await context.bot.send_voice(chat_id, audio)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # 每 5 小時推播一次（18000 秒）
    app.job_queue.run_repeating(active_push, interval=18000, first=10)

    print("🚀 Congyin V7.2 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
