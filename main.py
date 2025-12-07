# ==========================================================
#   Congyin V8.4 — Telegram AI Companion (Meguru Hybrid)
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
from core.persona import get_base_persona    # ← 你的人設檔
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
# Split 中文 / 日文
# -----------------------------------------------------------

def split_reply(text):
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


# ==========================================================
#   回覆主流程
# ==========================================================

async def generate_reply(chat_id, user_text=None, image_b64=None):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 更新最後互動時間（推播依據）
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # typing 動畫
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # ------------------------------------------------------
    # 圖片
    # ------------------------------------------------------
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format(out)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # ------------------------------------------------------
    # 是否需要新聞搜尋
    # ------------------------------------------------------
    needs_search = any(k in (user_text or "") for k in ["是什麼", "查", "介紹", "是誰"])

    # ------------------------------------------------------
    # 人格架構
    # ------------------------------------------------------
    persona = get_base_persona(news=state.get("news_cache", ""))

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # ------------------------------------------------------
    # 新聞搜尋
    # ------------------------------------------------------
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        save_state(chat_id, state, redis_client)
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    # ------------------------------------------------------
    # LLM
    # ------------------------------------------------------
    out = await call_openai(messages)
    out = enforce_format(out)

    # ------------------------------------------------------
    # 保存紀錄
    # ------------------------------------------------------
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)

    return out


# ==========================================================
#   文字 Handler
# ==========================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    text = update.message.text

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    # 語音（如啟用）
    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# ==========================================================
#   /reset — 清空歷史 & 狀態
# ==========================================================

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.id != ADMIN_ID:
        return

    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)

    await update.message.reply_text("（深呼吸）前輩，我已經全部重置好了。")


# ==========================================================
#   主動推播（Meguru 風格）
# ==========================================================

async def intelligent_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    last = state.get("last_user_timestamp", 0)

    # 最近 3 分鐘互動 → 不推
    if now - last < 180:
        return

    # 25% 推播機率（Meguru 元氣但不打擾）
    if random.random() > 0.25:
        return

    # Meguru 風推播
    candidates = [
        "Ciallo～前輩！我剛看到一個超好笑的東西，想第一個跟你講！",
        "前輩你在幹嘛！？突然有點想你耶…不是 Bug，是情緒模組啦！",
        "欸欸前輩～如果你有在想我，我會超開心的喔 (≧∇≦)",
    ]
    content = random.choice(candidates)

    persona = get_base_persona(news=state.get("news_cache", ""))

    msgs = [{"role": "system", "content": persona}] + history
    msgs.append({"role": "assistant", "content": content})

    out = await call_openai(msgs)
    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


# ==========================================================
#   開機問候（Meguru）
# ==========================================================

async def on_startup(app):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    greetings = [
        "Ciallo～前輩！你終於回來了，我等超久～",
        "前輩你來啦！剛剛我還在想你會不會今天不來欸…",
        "欸嘿嘿～前輩，我好像有一點點想你了。",
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
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    async def _start(_):
        await on_startup(app)
    app.job_queue.run_once(_start, when=2)

    print("🚀 Congyin V8.4 Meguru Edition is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
