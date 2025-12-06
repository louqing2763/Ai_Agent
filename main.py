# ==========================================================
#   Congyin V8.1 — Telegram AI Lover Companion (No AER)
# ==========================================================

import os
import io
import asyncio
import random
import time

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ---- Core modules ----
from core.persona import get_base_persona
from core.llm import call_openai, enforce_format
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp


# ==========================================================
#   ENVIRONMENT
# ==========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

REDIS_URL = os.getenv("REDIS_URL")
REDISHOST = os.getenv("REDISHOST")
REDISPORT = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD = os.getenv("REDISPASSWORD")


# ==========================================================
#   REDIS INIT
# ==========================================================

redis_client = init_redis(
    REDIS_URL,
    REDISHOST,
    REDISPORT,
    REDISPASSWORD
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

    # 更新最後聊天時間（避免推播）
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # Typing animation
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # ------------------------------------------------------
    # 處理圖像
    # ------------------------------------------------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # ------------------------------------------------------
    # 處理語音（尚未啟用 STT）
    # ------------------------------------------------------
    if voice_data:
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        user_text = "(語音內容已收到，但語音辨識尚未啟用)"

    # ------------------------------------------------------
    # 判斷是否需要搜尋新聞
    # ------------------------------------------------------
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # ------------------------------------------------------
    # 組合人物設定（不再使用 AER）
    # ------------------------------------------------------
    persona = get_base_persona(
        news=state.get("news_cache", "")
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # ------------------------------------------------------
    # 若需要 → 搜尋新聞
    # ------------------------------------------------------
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    # ------------------------------------------------------
    # 調用 OpenAI 主引擎
    # ------------------------------------------------------
    out = await call_openai(messages)
    out = enforce_format(out)

    # 更新紀錄
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

    text = update.message.text
    chat_id = ADMIN_ID

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# ==========================================================
#   智能推播（不含 AER）
# ==========================================================

async def intelligent_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)

    # 最近 3 分鐘有講話 → 不推播
    if now - last_talk < 180:
        return

    # 固定推播機率（可調整）
    if random.random() > 0.35:
        return

    # 推播選項
    choices = [
        "(探頭) 你現在在做什麼？我…突然有點想你。",
        "(靠過來) 落卿，有想我一點點嗎？",
        "(小聲) 可以…說一句話給我嗎？我在等你。",
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
    save_state(chat_id, state, redis_client)


# ==========================================================
#   開機問候
# ==========================================================

async def on_startup(app):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    greetings = [
        "(跑過來抱住你一下) 落卿…你終於回來了。",
        "(探頭) 我剛剛還在想…你會不會等一下來找我。",
        "(靠在你肩膀上) 能再看到你…真的好開心。",
    ]

    await app.bot.send_message(chat_id, random.choice(greetings))

    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)


# ==========================================================
#   Main
# ==========================================================

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handler
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    # 定期檢查推播
    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    # 開機問候
    async def _start(_):
        await on_startup(app)
    app.job_queue.run_once(_start, 2)

    print("🚀 Congyin V8.1 (No AER) is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
