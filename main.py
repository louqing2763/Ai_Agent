# ==========================================================
# main.py — 現在完全不用修改語氣，只讀 persona_config
# ==========================================================

import os, io, asyncio, random, time
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

from core.persona import get_persona, PUSH_LINES
from core.llm import call_openai, enforce_format
from core.redis_store import (
    init_redis, save_history, load_history,
    save_state, load_state
)
from core.news import search_news
from core.vision import analyze_image
from core.tts import tts_jp


# ----------------------------------------------------------
# ENV
# ----------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

REDIS_URL = os.getenv("REDIS_URL")
REDISHOST = os.getenv("REDISHOST")
REDISPORT = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD = os.getenv("REDISPASSWORD")

redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# ----------------------------------------------------------
# 分割答案
# ----------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# ----------------------------------------------------------
# 回覆生成流程
# ----------------------------------------------------------

async def generate_reply(chat_id, user_text=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    persona = get_persona(state.get("news_cache", ""))

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    out = await call_openai(messages)
    out = enforce_format(out)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)

    return out


# ----------------------------------------------------------
# handle_text
# ----------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    text = update.message.text

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)


# ----------------------------------------------------------
# 推播
# ----------------------------------------------------------

async def intelligent_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    if now - state.get("last_user_timestamp", 0) < 180:
        return

    content = random.choice(PUSH_LINES["default"])

    persona = get_persona(state.get("news_cache", ""))
    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_openai(messages)
    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


# ----------------------------------------------------------
# reset
# ----------------------------------------------------------

async def cmd_reset(update: Update, context):

    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)

    await update.message.reply_text("(深呼吸)…好了，我重新開始了。")


# ----------------------------------------------------------
# Main
# ----------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    print("🚀 Congyin V8.4 (Unified Persona System) Running")
    app.run_polling()


if __name__ == "__main__":
    main()

