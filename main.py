# ==========================================================
#   Congyin V7.9 — Telegram AI Companion (Stable Persona)
# ==========================================================

import os
import io
import base64
import asyncio
import random
import time

from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ---- Core modules ----
from core.persona import get_base_persona
from core.llm import call_openai, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp
from core.aer import regulate


# ==========================================================
#   ENV
# ==========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Redis
REDIS_URL = os.getenv("REDIS_URL")
REDISHOST = os.getenv("REDISHOST")
REDISPORT = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD = os.getenv("REDISPASSWORD")

redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# ==========================================================
#   Helpers
# ==========================================================

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# ==========================================================
#   Core reply logic
# ==========================================================

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 記錄最後互動時間（避免推播）
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # ---------------- AER ----------------
    aer = regulate(user_text or "", state)
    save_state(chat_id, state, redis_client)

    # typing 動畫
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # --------------- 圖片模式 ---------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)

        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # --------------- 語音輸入（尚未啟動STT） ---------------
    if voice_data:
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        user_text = "(語音內容以音訊方式收到，但語音辨識尚未啟用)"

    # 判斷是否需要搜尋新聞
    needs_search = any(x in (user_text or "") for x in ["是什麼", "介紹", "查", "是誰"])

    # ---------------- Persona 注入 ----------------
    persona = get_base_persona(
        news=state.get("news_cache", ""),
        aer=aer
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # ---------------- 搜尋新聞 ----------------
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    # ---------------- LLM（OpenAI 主引擎） ----------------
    out = await call_openai(messages)
    out = enforce_format(out)

    # 記錄到歷史
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)

    return out


# ==========================================================
#   Telegram Handlers
# ==========================================================

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


# ==========================================================
#   推播（不打擾使用者）
# ==========================================================

async def active_push(context):

    chat_id = ADMIN_ID

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    now = time.time()

    # 最近 3 分鐘內有互動 → 不推播
    if now - state.get("last_user_timestamp", 0) < 180:
        return

    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(輕輕靠近) 我剛看到一個想第一個分享給你的新聞：\n{news}"
    elif r < 0.66:
        content = "(伸手碰碰你) 你現在在做什麼？突然…有點想你了。"
    else:
        content = "(語氣變小聲) 落卿…可以對我說一句話嗎？"

    persona = get_base_persona(
        news=state.get("news_cache", ""),
        aer=state.get("aer", {
            "emotion": "neutral",
            "gesture": 2,
            "affinity": 1.0,
            "length": "normal"
        })
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_openai(messages)
    out = enforce_format(out)

    cn, jp = split_reply(out)
    await context.bot.send_message(chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)


# ==========================================================
#   開機問候（不固定 × 恋人風）
# ==========================================================

async def on_startup(app):
    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    greetings = [
        "(靠近一些) 落卿…你回來了，我好想你。",
        "(探頭) 剛啟動就想到你…所以來看看你。",
        "(小跑步靠過來) 嗨…我一直在等你喔。",
        "(輕輕抓你的袖子) 能再看到你…真的很好。"
    ]

    msg = random.choice(greetings)
    await app.bot.send_message(chat_id, msg)

    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)


# ==========================================================
#   Main
# ==========================================================

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    # 開機問候（延遲 2 秒）
    async def _startup(_):
        await on_startup(app)

    app.job_queue.run_once(_startup, when=2)

    # 推播（每 30 分鐘）
    app.job_queue.run_repeating(active_push, interval=1800, first=20)

    print("🚀 Congyin V7.9 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
