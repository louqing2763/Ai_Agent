# ==========================================================
#   Congyin V7.3 — High-Realism Emotional AI Companion
#   Author: 落卿
# ==========================================================

import os
import io
import base64
import asyncio
import random
from datetime import datetime

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ------------------- modules -------------------------------
from core.persona import get_base_persona
from core.llm import call_openai, call_deepseek, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp

# AER 系統（短期＋長期情緒）
from core.aer import generate_AER
from core.emotion import regulate


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


# ---------------------- Redis Init ------------------------------

redis_client = init_redis(
    REDIS_URL,
    REDISHOST,
    REDISPORT,
    REDISPASSWORD
)


# ---------------------- 工具函式 ------------------------------

def split_reply(text):
    """切割中日文回覆"""
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


def init_aer_state(state):
    """初始化 AER 狀態（若不存在）"""
    if "aer" not in state:
        state["aer"] = {
            "emotion": "neutral",
            "gesture": 1,
            "affinity": 1.0,
            "length": "normal",
            "last_timestamp": 0
        }
    return state


# --------------------------------------------------------------
# 產生回覆（主要邏輯）
# --------------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 初始化 AER 狀態
    state = init_aer_state(state)

    # 輸入中動畫
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # ------------------- 圖片模式 -------------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)

        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # ------------------- 語音模式 -------------------
    if voice_data:
        user_text = "(語音轉文字尚未啟用)"

    # ------------------- 情緒系統（短期 AER） -------------------
    short_term_aer = generate_AER(user_text, state["aer"])

    # ------------------- 情緒系統（長期人格漂移） -------------------
    long_term_aer = regulate(user_text, state["aer"])

    # ------------------- 合併成最終 AER -------------------
    final_AER = {
        "emotion": short_term_aer["emotion"],
        "gesture": max(short_term_aer["gesture"], long_term_aer["gesture"]),
        "affinity": (short_term_aer["affinity"] + long_term_aer["affinity"]) / 2,
        "length": short_term_aer["length"]
    }

    # 儲存 AER
    state["aer"].update(final_AER)

    # ------------------- 是否需要搜尋 -------------------
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # ------------------- 組裝 persona -------------------
    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = state["aer"]
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

    # 儲存歷史
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
    state = init_aer_state(state)

    # 語音模式切換
    if "開啟語音" in text:
        state["voice_mode"] = True
        save_state(chat_id, state, redis_client)
        await update.message.reply_text("(語音模式已啟動)")
        return

    if "關閉語音" in text:
        state["voice_mode"] = False
        save_state(chat_id, state, redis_client)
        await update.message.reply_text("(語音模式已關閉)")
        return

    # 生成回覆
    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    # 語音模式
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


async def handle_photo(update: Update, context):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    state = init_aer_state(state)

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
    state = init_aer_state(state)

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
# 推播（會影響情緒）
# --------------------------------------------------------------

async def active_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    state = init_aer_state(state)

    history = load_history(chat_id, redis_client)

    if state.get("sleeping"):
        return

    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(輕快跑來) 給你看我剛看到的：\n{news}"
    elif r < 0.66:
        content = "(探頭) 你現在在做什麼？我有點想你。"
    else:
        content = "(靠近) 可以跟我說一句話嗎？我好像……有點想聽你的聲音。"

    # 推播時，親密度稍微提升（她主動找你）
    state["aer"]["affinity"] = min(2.0, state["aer"]["affinity"] + 0.03)

    persona = get_base_persona(
        news = state.get("news_cache", ""),
        aer = state["aer"]
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

    # 每五小時推播
    app.job_queue.run_repeating(active_push, interval=18000, first=10)

    print("🚀 Congyin V7.3 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
