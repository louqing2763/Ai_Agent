# ==========================================================
#   Congyin V7.0 — Telegram AI Companion (Modular Version)
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
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ---- modules -------------------------------------------------
from core.persona import get_base_persona
from core.llm import call_openai, call_deepseek, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.utils import now_taipei
from core.vision import analyze_image
from core.tts import tts_jp

# ------------------------ ENV --------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY    = os.getenv("DEEPSEEK_API_KEY")
print("DEEPSEEK_API_KEY =", DEEPSEEK_API_KEY)
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

print("Redis client =", redis_client)

# --------------------------------------------------------------
# Split 中文 / 日文
# --------------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()
# =============================================================
# AER — 自動情緒調節器
# =============================================================

import math
import time

def infer_emotion(text: str):
    """依訊息判斷情緒強度: low / neutral / high"""

    if not text:
        return "neutral"

    # 判斷低氣壓
    low_keywords = ["不好", "累", "煩", "厭", "算了", "不想", "沒力氣", "哭", "心情差"]
    if any(k in text for k in low_keywords):
        return "low"

    # 判斷高能量
    high_keywords = ["哈哈", "ww", "太好", "好爽", "耶", "超棒", "!!", "?!", "期待"]
    if any(k in text for k in high_keywords):
        return "high"

    return "neutral"


def infer_affinity(history, last_timestamp):
    """
    根據最近互動時間與聊天流暢度推算親密度 (0.0 ~ 1.5)
    你越常跟她講話，她越靠近（Affinity 上升）
    你很久不講話，她會比平常更貼心但稍稍變得安靜（Affinity 稍降）
    """

    now = time.time()
    silence = now - last_timestamp  # 秒

    # 基礎親密度
    affinity = 1.0

    # 若沉默超過 20 分鐘 → 稍微下降
    if silence > 1200:
        affinity -= 0.2

    # 若沉默超過 2 小時 → 更低
    if silence > 7200:
        affinity -= 0.4

    # 互動越頻繁 → 上升
    if len(history) >= 10:
        affinity += 0.1
    if len(history) >= 30:
        affinity += 0.15

    # 限制範圍
    return max(0.5, min(1.5, affinity))


def infer_gesture_level(emotion, affinity):
    """
    根據她對落卿的親密度 & 你的情緒，決定動作量：
    0 = 幾乎沒動作（怕你不舒服）
    1 = 一個小動作
    2 = 1-2 個動作
    3 = 2-3 個動作（很黏、很靠近）
    """

    if emotion == "low":
        return 1 if affinity < 1.0 else 2   # 若你低落，她會更貼心

    if emotion == "high":
        return 2 if affinity < 1.0 else 3

    # neutral
    if affinity > 1.3:
        return 3
    if affinity > 1.0:
        return 2
    return 1


def infer_reply_length(emotion):
    """決定回覆長度 short / normal / long"""
    if emotion == "low":
        return "long"   # 你低落 → 她會多說話
    if emotion == "high":
        return "normal"
    return "normal"


# --------------------------------------------------------------
# 產生回覆（主處理流程）
# --------------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 優化：顯示「輸入中」
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

    # ------------------- 語音 → 文字 -------------------
    if voice_data:
        # 避免讓 Whisper 成為 bottleneck → 先簡化流程
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        text = "(語音轉文字已關閉：如需啟用可再加入)"
        user_text = text

    # ------------------- 搜尋觸發 -------------------
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # ------------------- 組裝人物設定 -------------------
    persona = get_base_persona(state.get("news_cache", ""))

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

    # 切換語音模式
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

    # 主要回覆
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

    f = await update.message.photo[-1].get_file()
    data = await f.download_as_bytearray()
    b64 = base64.b64encode(data).decode()

    out = await generate_reply(chat_id, image_b64=b64)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


async def handle_voice(update: Update, context):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID

    f = await update.message.voice.get_file()
    data = await f.download_as_bytearray()

    out = await generate_reply(chat_id, voice_data=data)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# --------------------------------------------------------------
# Push System（不限次數推播）
# --------------------------------------------------------------

async def active_push(context):
    chat_id = ADMIN_ID

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 若 chatbot 在睡眠 → 不推播
    if state.get("sleeping"):
        return

    # 推播類型
    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(輕快地跑過來) 給你看個我剛看到的：\n{news}"

    elif r < 0.66:
        content = "(探頭) 你現在在做什麼？有一點點想你。"

    else:
        content = "(靠近) 可以跟我說一句話嗎？我想聽。"

    persona = get_base_persona(state.get("news_cache", ""))
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

    # 每 5 分鐘推播一次
    app.job_queue.run_repeating(active_push, interval=300, first=10)

    print("🚀 Congyin V7.0 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()



