# ==========================================================
#   Congyin V8.0 — Telegram AI Lover Companion (Fixed)
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
from core.aer import regulate


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

    # 記錄最後聊天時間
    state["last_user_timestamp"] = time.time()

    # AER 更新
    aer = regulate(user_text or "", state)
    state["aer"] = aer
    save_state(chat_id, state, redis_client)

    # Typing animation
    try:
        await app.bot.send_chat_action(chat_id, "typing")
    except:
        pass

    # 圖片模式
    if image_b64:
        out = enforce_format(await analyze_image(image_b64))
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 語音（尚未啟動 STT）
    if voice_data:
        audio = io.BytesIO(voice_data)
        audio.name = "voice.ogg"
        user_text = "(語音內容已收到，但語音辨識未啟用)"

    # 判斷是否搜尋
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # 人格組裝
    persona = get_base_persona(
        news=state.get("news_cache", ""),
        aer=aer
    )

    # 注入 AER 狀態 → 保證情緒承接
    aer_system = f"(AER 狀態) {aer}"

    messages = [
        {"role": "system", "content": persona},
        {"role": "system", "content": aer_system},
    ] + history

    messages.append({"role": "user", "content": user_text})

    # 搜尋新聞
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        save_state(chat_id, state, redis_client)
        messages.append({"role": "system", "content": f"(搜尋結果){news}"})

    # 調用 OpenAI
    out = enforce_format(await call_openai(messages, affinity=aer["affinity"]))

    # 更新歷史
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
    user_text = update.message.text

    out = await generate_reply(chat_id, user_text=user_text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    # 語音模式
    state = load_state(chat_id, redis_client)
    if state.get("voice_mode"):
        audio = tts_jp(jp, ELEVEN_API_KEY, ELEVEN_VOICE_ID)
        if audio:
            await update.message.reply_voice(audio)


# ==========================================================
#   V8.0 Intelligent Push（根據親密度）
# ==========================================================

async def intelligent_push(context):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)

    # 最近 3 分鐘有聊天 → 不推播
    if now - last_talk < 180:
        return

    # 讀取 AER 親密度
    affinity = state.get("aer", {}).get("affinity", 1.2)

    # 根據親密度調整推播機率
    if affinity > 1.5:
        push_rate = 0.50
    elif affinity >= 1.0:
        push_rate = 0.30
    else:
        push_rate = 0.10

    # 機率判定
    if random.random() > push_rate:
        return

    # 推播內容
    r = random.random()

    if r < 0.33:
        news = await search_news()
        state["news_cache"] = news
        content = f"(探頭) 我剛看到這個新聞…想第一個告訴你：\n{news}"
    elif r < 0.66:
        content = "(小小靠過你) 你現在在做什麼？突然…有點想你了。"
    else:
        content = "(輕聲) 落卿…可以說一句話給我嗎？我想聽你。"

    # 人格
    persona = get_base_persona(
        news=state.get("news_cache", ""),
        aer=state.get("aer", {
            "emotion": "neutral",
            "gesture": 2,
            "affinity": affinity,
            "length": "normal"
        })
    )

    aer_system = f"(AER 狀態) {state.get('aer', {})}"

    messages = [
        {"role": "system", "content": persona},
        {"role": "system", "content": aer_system},
    ] + history

    messages.append({"role": "assistant", "content": content})

    out = enforce_format(await call_openai(messages, affinity=affinity))

    cn, jp = split_reply(out)
    await context.bot.send_message(chat_id, cn)

    # 更新歷史
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)


# ==========================================================
#   開機問候（不固定）
# ==========================================================

async def on_startup(app):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)

    greetings = [
        "(跑過來抱住你一下) 落卿…你回來了，我好想你。|||（駆け寄ってぎゅっとする）落卿…帰ってきた、会いたかったよ。",
        "(探頭) 你醒了嗎？我剛剛還在想你會不會來找我。|||（ひょこ）起きた？さっきまで、来てくれるかなって思ってた。",
        "(輕輕碰你手背) 嗨…我一直在等你喔。|||（そっと手に触れる）ねぇ…ずっと待ってたよ。",
        "(靠在你肩上) 再看到你…真的很開心。|||（肩に寄りかかる）また君に会えて…ほんとに嬉しい。",
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

    # 推播：每 30 分鐘檢查一次
    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    # 開機問候
    async def _start(_):
        await on_startup(app)

    app.job_queue.run_once(_start, when=2)

    print("🚀 Congyin V8.0 (fixed) is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
