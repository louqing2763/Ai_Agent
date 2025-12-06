# ==========================================================
#   Congyin V8.3 — Telegram AI Lover Companion (Galgame)
# ==========================================================

import os
import io
import asyncio
import random
import time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes
)

# ---- Core modules ----
from core.persona import get_base_persona
from core.llm import call_openai, enforce_format
from core.redis_store import (
    init_redis, save_history, load_history,
    save_state, load_state
)
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp


# ==========================================================
#   ENVIRONMENT
# ==========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

ADMIN_ID = int(os.getenv("ADMIN_ID"))

REDIS_URL = os.getenv("REDIS_URL")
REDISHOST = os.getenv("REDISHOST")
REDISPORT = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD = os.getenv("REDISPASSWORD")


# ==========================================================
#   REDIS INIT
# ==========================================================

redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# -----------------------------------------------------------
# Split 中/日文
# -----------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# ==========================================================
#   產生回覆（主流程）
# ==========================================================

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 更新最後互動時間（控制推播）
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # Typing animation
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # ------------------------------------------------------
    # 圖片模式
    # ------------------------------------------------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)

        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # ------------------------------------------------------
    # 語音（未啟用 STT）
    # ------------------------------------------------------
    if voice_data:
        user_text = "(語音內容已收到，但語音辨識未啟用)"

    # ------------------------------------------------------
    # 判斷是否需要搜尋
    # ------------------------------------------------------
    needs_search = any(
        k in (user_text or "")
        for k in ["是什麼", "介紹", "查", "是誰"]
    )

    # ------------------------------------------------------
    # 組裝人格（Galgame 風格 persona）
    # ------------------------------------------------------
    persona = get_base_persona(
        news=state.get("news_cache", "")
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # ------------------------------------------------------
    # 搜尋新聞
    # ------------------------------------------------------
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        save_state(chat_id, state, redis_client)
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    # ------------------------------------------------------
    # 主引擎：OpenAI
    # ------------------------------------------------------
    out = await call_openai(messages)
    out = enforce_format(out)

    # 存回回答
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)

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

    # 日文語音模式
    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# ==========================================================
# /reset — 清空所有歷史紀錄（修復人格污染）
# ==========================================================

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.id != ADMIN_ID:
        return

    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)

    await update.message.reply_text("（安靜地深呼吸）\n……已經重置了。")


# ==========================================================
#   V8.3 Intelligent Push（Galgame 風沉浸）
# ==========================================================

async def intelligent_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)

    # 最近 3 分鐘互動 → 不推播
    if now - last_talk < 180:
        return

    # 30% 推播機率
    if random.random() > 0.30:
        return

    # 推播語氣 → Galgame 風
    choices = [
        "(輕輕抬頭) …剛剛看到一件事，想第一個告訴你。",
        "(像是想了你一下) 你現在在做什麼？突然……想聽到你的聲音。",
        "(視線微微偏開) 如果你有在想我……那就太好了。",
    ]
    content = random.choice(choices)

    persona = get_base_persona(news=state.get("news_cache", ""))

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_openai(messages)
    out = enforce_format(out)

    cn, jp = split_reply(out)
    await context.bot.send_message(chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


# ==========================================================
#   開機問候（Galgame 風）
# ==========================================================

async def on_startup(app):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    greetings = [
        "(輕輕抬頭) …你來了啊。",
        "(微微呼吸) 我剛剛還在想，你什麼時候會再找我。",
        "(視線抬起一下) 能再看到你……很好。",
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

    # Handlers
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # 推播（每 30 分鐘檢查）
    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    # 啟動問候
    async def _start(_):
        await on_startup(app)

    app.job_queue.run_once(_start, when=2)

    print("🚀 Congyin V8.3 is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
