# ==========================================================
# main.py — DeepSeek + Anti-Repetition + Timer
# ==========================================================

import os, io, asyncio, random, time, contextlib, requests, difflib
import datetime
import pytz

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

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

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# ==========================================================
# ✨ Anti-Repetition Modules
# ==========================================================

def is_too_similar(text1, text2, threshold=0.92):
    """判斷兩段回覆是否過於雷同"""
    if not text1 or not text2:
        return False
    ratio = difflib.SequenceMatcher(None, text1, text2).ratio()
    return ratio > threshold


def get_last_assistant_reply(history):
    """取得上一句 assistant 回覆"""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            return msg.get("content")
    return None


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
# DeepSeek Wrapper（主大腦）
# ----------------------------------------------------------

async def call_deepseek(messages):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.95,
    }

    res = await asyncio.to_thread(
        requests.post, url, headers=headers, json=payload
    )
    data = res.json()
    return data["choices"][0]["message"]["content"]


# ----------------------------------------------------------
# 格式整理
# ----------------------------------------------------------

def enforce_format_simple(text):
    if not text:
        return "…（無內容）"
    return text.strip()


# ----------------------------------------------------------
# 回覆生成流程（加入防同質化 + timer_flag）
# ----------------------------------------------------------

async def generate_reply(
    chat_id,
    user_text=None,
    image_b64=None,
    voice_data=None,
    context=None,
    from_timer: bool = False,
):

    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    now = time.time()
    last_talk = state.get("last_user_timestamp", now)
    minutes_since_last = int((now - last_talk) / 60)

    # 計時器提醒旗標（由 timer callback 設定）
    timer_flag = state.get("timer_trigger", False)

    # 只有「使用者說話」才更新 last_user_timestamp
    if not from_timer:
        state["last_user_timestamp"] = now
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

        # 判斷是否需搜尋
        needs_search = any(
            k in (user_text or "")
            for k in ["是什麼", "介紹", "查", "是誰"]
        )

        persona = get_persona(
            news=state.get("news_cache", "今天沒有新聞。"),
            minutes_since_last=minutes_since_last,
            timer_trigger=timer_flag,
        )

        messages = [{"role": "system", "content": persona}] + history
        messages.append({"role": "user", "content": user_text})

        if needs_search:
            news = await search_news()
            state["news_cache"] = news
            save_state(chat_id, state, redis_client)
            messages.append({"role": "system", "content": f"(搜尋結果){news}"})

        # 主回覆
        out = await call_deepseek(messages)
        out = enforce_format_simple(out)

        # ✨ 防同質化：與上一句過於相似 → 重生一次
        last_reply = get_last_assistant_reply(history)
        if is_too_similar(out, last_reply):
            out = await call_deepseek(messages)
            out = enforce_format_simple(out)

    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    # 清除 timer_flag（如果有的話）
    if timer_flag:
        state["timer_trigger"] = False
        save_state(chat_id, state, redis_client)

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
# 推播（LLM 生成，不使用固定句）
# ----------------------------------------------------------

async def intelligent_push(context: ContextTypes.DEFAULT_TYPE):

    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    # ---- 台灣時間判斷 ----
    tz = pytz.timezone("Asia/Taipei")
    now_dt = datetime.datetime.now(tz)
    hour = now_dt.hour

    # 1) 夜間靜音：23:00 ~ 08:00
    if hour >= 23 or hour < 8:
        return

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)
    minutes_since_last = int((now - last_talk) / 60)

    # 2) 3 分鐘內有互動 → 不推播
    if now - last_talk < 180:
        return

    # 3) 超過 2 小時完全不推播（避免刷存在感）
    if now - last_talk > 2 * 3600:
        return

    # 4) 讓 LLM 生成推播句子
    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=False,
    )

    push_instruction = (
        "請生成一行簡短推播訊息，必須符合 persona 中的「推播輸出限制」。"
        "不可多段、不可故事化、不可超過 35 字，只能一句調皮、主動、活潑的少女語氣句子。"
    )

    messages = [
        {"role": "system", "content": persona},
        {"role": "user", "content": push_instruction},
    ]

    out = await call_deepseek(messages)
    out = enforce_format_simple(out)

    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


# ----------------------------------------------------------
# Timer 功能
# ----------------------------------------------------------

async def timer_done(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback：計時器時間到，由聰音出面提醒。"""
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    label = data.get("label", "")

    if chat_id is None:
        return

    # 設定 timer_trigger 旗標，讓 persona 知道這是「計時提醒」
    state = load_state(chat_id, redis_client)
    state["timer_trigger"] = True
    state["last_timer_label"] = label
    save_state(chat_id, state, redis_client)

    # 由 generate_reply 產生「時間到了」的聰音風格回覆
    user_text = "計時器時間到了。" + (f"（項目：{label}）" if label else "")
    out = await generate_reply(
        chat_id,
        user_text=user_text,
        from_timer=True,
    )
    cn, jp = split_reply(out)

    await context.bot.send_message(chat_id, cn)


async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /timer <分鐘數> [說明]
    例：/timer 3 泡麵
        /timer 25 專心寫程式
    """
    if update.effective_chat.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("用法：/timer <分鐘數> [說明]\n例如：/timer 3 泡麵")
        return

    # 解析分鐘數
    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text("請給我一個正整數分鐘，例如：/timer 5 泡麵")
        return

    if minutes <= 0:
        await update.message.reply_text("計時器時間必須大於 0。")
        return

    if minutes > 24 * 60:
        await update.message.reply_text("單次計時器不能超過 1440 分鐘（24 小時）。")
        return

    label = " ".join(context.args[1:]).strip()
    chat_id = ADMIN_ID

    # 建立 Job
    context.job_queue.run_once(
        timer_done,
        when=minutes * 60,
        data={"chat_id": chat_id, "label": label},
    )

    # 紀錄在 state（純備查，之後要顯示「正在計時列表」也可用）
    state = load_state(chat_id, redis_client)
    timers = state.get("timers", [])
    timers.append({
        "label": label,
        "minutes": minutes,
        "start_ts": time.time(),
    })
    state["timers"] = timers
    save_state(chat_id, state, redis_client)

    msg = f"已設定 {minutes} 分鐘計時器。"
    if label:
        msg += f"（項目：{label}）"
    await update.message.reply_text(msg)


# ----------------------------------------------------------
# reset
# ----------------------------------------------------------

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return

    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)
    await update.message.reply_text("（系統已重置）")


# ----------------------------------------------------------
# main
# ----------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("timer", cmd_timer))

    # 每 30 分鐘檢查一次是否要推播
    app.job_queue.run_repeating(intelligent_push, interval=1800, first=20)

    print("🚀 Congyin V8.6 — DeepSeek + Anti-Repetition + Timer Running")
    app.run_polling()


if __name__ == "__main__":
    main()
