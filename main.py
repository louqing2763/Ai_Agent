# ==========================================================
# main.py (DeepSeek + Anti-Repetition + Safe JobQueue)
# ==========================================================

import os, io, asyncio, random, time, contextlib, requests, difflib
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
    except Exception:
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
    """
    呼叫 DeepSeek API，但加入強韌錯誤處理：
    - API 回傳非 JSON → 不 crash
    - 連線失敗 → 給 fallback 訊息
    """

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

    try:
        res = await asyncio.to_thread(
            requests.post, url, headers=headers, json=payload, timeout=20
        )
    except Exception as e:
        return f"(DeepSeek 連線失敗: {e})"

    # 如果 status code 錯誤 → 回傳 API 錯誤內容
    if res.status_code != 200:
        return f"(DeepSeek API 錯誤 {res.status_code}) 回應: {res.text[:200]}"

    # 嘗試解析 JSON
    try:
        data = res.json()
    except Exception:
        return f"(DeepSeek 回傳非 JSON) 回應: {res.text[:200]}"

    # 正常輸出
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return f"(DeepSeek 回傳結構異常) data: {data}"

# ----------------------------------------------------------
# 格式整理
# ----------------------------------------------------------

def enforce_format_simple(text):
    if not text:
        return "…（無內容）"
    return text.strip()


# ----------------------------------------------------------
# 回覆生成流程（含防同質化 + minutes_since_last）
# ----------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None, context=None):
    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 計算距離上一次對話經過幾分鐘（在覆蓋前先算）
    last_ts = state.get("last_user_timestamp")
    if last_ts:
        minutes_since_last = int((time.time() - last_ts) / 60)
    else:
        minutes_since_last = None

    # 更新最後對話時間
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    typing_task = asyncio.create_task(send_typing(chat_id))

    try:
        # 圖片模式
        if image_b64:
            out = await analyze_image(image_b64)
            out = enforce_format_simple(out)
            history.append({"role": "assistant", "content": out})
            save_history(chat_id, history, redis_client)
            return out

        # 語音模式（目前僅佔位）
        if voice_data:
            user_text = "(語音內容接收，但語音辨識未啟用)"

        # 判斷是否需要搜尋新聞
        needs_search = any(
            k in (user_text or "")
            for k in ["是什麼", "介紹", "查", "是誰"]
        )

        # persona 會帶入 minutes_since_last；timer_trigger 目前先固定 False
        persona = get_persona(
            news=state.get("news_cache", "今天沒有新聞。"),
            minutes_since_last=minutes_since_last,
            timer_trigger=False,
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

        # === 防同質化：如果跟上一句太像，就重生一次 ===
        last_reply = get_last_assistant_reply(history)
        if is_too_similar(out, last_reply):
            out = await call_deepseek(messages)
            out = enforce_format_simple(out)

    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    # 儲存新回覆
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

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)

    # 夜間靜音：23:00 ~ 08:00 不推播
    lt = time.localtime(now)
    hour = lt.tm_hour
    if hour >= 23 or hour < 8:
        return

    # 最近 3 分鐘內有互動 → 不推播
    if now - last_talk < 180:
        return

    # 超過 2 小時完全沒互動 → 也不再主動刷存在感
    if now - last_talk > 2 * 3600:
        return

    # 距離上次對話的分鐘數（給 persona）
    minutes_since_last = int((now - last_talk) / 60) if last_talk else None

    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=False,
    )

    push_instruction = (
        "請生成**一行推播訊息**，必須符合 persona 中的『推播限制規則』："
        "不可多段、不可故事化、不可超過 35 字，只能一句簡短、"
        "調皮、主動、活潑的少女語氣。"
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
# reset
# ----------------------------------------------------------

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)
    await update.message.reply_text("（系統已重置）")


# ----------------------------------------------------------
# main
# ----------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # ⚠ JobQueue 在某些環境下可能是 None，先檢查再註冊
    jq = getattr(app, "job_queue", None)
    if jq is not None:
        jq.run_repeating(intelligent_push, interval=1800, first=20)
        print("✅ JobQueue 啟用：已註冊 intelligent_push")
    else:
        print("⚠ JobQueue 未啟用：不會進行推播（intelligent_push）")

    print("🚀 Congyin V8.6 — DeepSeek Version + Anti-Repetition Running")
    app.run_polling()


if __name__ == "__main__":
    main()

