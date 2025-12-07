# ==========================================================
# main.py — 不需要 llm.py，直接呼叫 OpenAI API
# ==========================================================

import os, io, asyncio, random, time, contextlib
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

from openai import OpenAI
from core.persona_config import get_persona, PUSH_LINES
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# ----------------------------------------------------------
# Typing animation
# ----------------------------------------------------------

async def send_typing(chat_id):
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass


# ----------------------------------------------------------
# 分割答案
# ----------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# ----------------------------------------------------------
# ✨ 直接呼叫 OpenAI (取代 llm.py)
# ----------------------------------------------------------

async def call_openai_direct(messages):
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.9,
    )
    return response.choices[0].message.content
# ----------------------------------------------------------
# 格式強制（過去 enforce_format 的簡化版）
# ----------------------------------------------------------

def enforce_format_simple(text):
    if not text:
        return "…（系統沒有回應內容）"
    return text.strip()


# ----------------------------------------------------------
# 回覆生成流程
# ----------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None, context=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    typing_task = asyncio.create_task(send_typing(chat_id))

    try:
        # 圖片
        if image_b64:
            out = await analyze_image(image_b64)
            out = enforce_format_simple(out)
            history.append({"role": "assistant", "content": out})
            save_history(chat_id, history, redis_client)
            return out

        # 語音
        if voice_data:
            user_text = "(語音內容接收，但語音辨識未啟用)"

        # 判斷是否需要搜尋
        needs_search = any(
            k in (user_text or "")
            for k in ["是什麼", "介紹", "查", "是誰"]
        )

        persona = get_persona(news=state.get("news_cache", ""))

        messages = [{"role": "system", "content": persona}] + history
        messages.append({"role": "user", "content": user_text})

        if needs_search:
            news = await search_news()
            state["news_cache"] = news
            save_state(chat_id, state, redis_client)
            messages.append({"role": "system", "content": f"(搜尋結果){news}"})


        # ✨ 不用 llm.py，直接呼叫 OpenAI
        out = await call_openai_direct(messages)
        out = enforce_format_simple(out)

    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    # 儲存回覆
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

    persona = get_persona(news=state.get("news_cache", ""))
    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "assistant", "content": content})

    out = await call_openai_direct(messages)
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

    print("🚀 Congyin V8.4 — No-llm.py Version Running")
    app.run_polling()


if __name__ == "__main__":
    main()


