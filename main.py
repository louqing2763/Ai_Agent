# ==========================================================
# main.py (DeepSeek + Anti-Repetition + Human-like Bubbles)
# ==========================================================

import os, io, asyncio, random, time, contextlib, requests, difflib
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

from core.persona_config import get_persona
from core.redis_store import (
    init_redis, save_history, load_history,
    save_state, load_state
)
from core.news import search_news
from core.vision import analyze_image

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
# ✨ Human-like Bubble Sender (擬人化氣泡發送)
# ----------------------------------------------------------

async def send_message_in_bubbles(bot, chat_id, full_text):
    """
    將完整的 AI 回覆文字，依照換行符號切分，
    模擬真人打字節奏，一句一句發送。
    """
    if not full_text:
        return

    # 1. 依照換行切分 (過濾掉空行)
    segments = [seg.strip() for seg in full_text.split('\n') if seg.strip()]

    for i, segment in enumerate(segments):
        # 2. 計算模擬延遲時間
        # 基礎延遲 0.3 秒 + 每個字 0.05 ~ 0.08 秒的浮動時間
        # 這樣長句子會打比較久，短句子會秒回
        char_delay = 0.05 + random.uniform(0, 0.03)
        delay = 0.5 + (len(segment) * char_delay)

        # 設定上限，避免長文讓使用者等太久 (最長等待 3.5 秒)
        delay = min(delay, 3.5)

        # 第一句如果是極短句 (如: 嗯、好)，縮短延遲以營造「秒回」感
        if i == 0 and len(segment) < 3:
            delay = 0.5

        # 3. 顯示「正在輸入...」狀態
        try:
            await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        except Exception:
            pass

        # 4. 執行延遲
        await asyncio.sleep(delay)

        # 5. 發送該段落
        try:
            await bot.send_message(chat_id=chat_id, text=segment)
        except Exception as e:
            print(f"Error sending segment: {e}")


# ----------------------------------------------------------
# 分割答案 (保留舊有邏輯)
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
        "temperature": 1.1, # 稍微調高溫度增加感性
        "max_tokens": 500,  # 限制輸出長度，避免過長
    }

    try:
        res = await asyncio.to_thread(
            requests.post, url, headers=headers, json=payload, timeout=30
        )
    except Exception as e:
        return f"(DeepSeek 連線失敗: {e})"

    if res.status_code != 200:
        return f"(DeepSeek API 錯誤 {res.status_code}) 回應: {res.text[:200]}"

    try:
        data = res.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return f"(DeepSeek 回傳異常) text: {res.text[:200]}"


# ----------------------------------------------------------
# 格式整理
# ----------------------------------------------------------

def enforce_format_simple(text):
    if not text:
        return "…"
    return text.strip()


# ----------------------------------------------------------
# 回覆生成流程
# ----------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):
    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 計算距離上一次對話經過幾分鐘
    last_ts = state.get("last_user_timestamp")
    if last_ts:
        minutes_since_last = int((time.time() - last_ts) / 60)
    else:
        minutes_since_last = None

    # 更新最後對話時間
    state["last_user_timestamp"] = time.time()
    save_state(chat_id, state, redis_client)

    # 圖片模式
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format_simple(out)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 判斷是否需要搜尋新聞
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=False,
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    if needs_search:
        try:
            # 顯示正在搜尋狀態，因為搜尋通常比較久
            # 這裡無法直接 await bot action，因為 generate_reply 是純邏輯層
            # 但我們可以先不處理，讓使用者等一下
            news = await search_news()
            state["news_cache"] = news
            save_state(chat_id, state, redis_client)
            messages.append({"role": "system", "content": f"(搜尋結果){news}"})
        except Exception as e:
            print(f"Search failed: {e}")

    # 主回覆
    out = await call_deepseek(messages)
    out = enforce_format_simple(out)

    # 防同質化
    last_reply = get_last_assistant_reply(history)
    if is_too_similar(out, last_reply):
        print("Trigger anti-repetition, regenerating...")
        out = await call_deepseek(messages)
        out = enforce_format_simple(out)

    # 儲存完整回覆 (在 History 裡存完整的，顯示時才切分)
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)

    return out


# ----------------------------------------------------------
# handle_text (修改版：使用氣泡發送)
# ----------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    text = update.message.text

    # 1. 為了讓使用者知道收到訊息了，先顯示一次 Typing
    await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

    # 2. 生成回覆 (這是一個完整的長字串)
    out = await generate_reply(chat_id, user_text=text)
    
    # 3. 分割 CN/JP (如果有的話)
    cn, jp = split_reply(out)

    # 4. 使用氣泡模式發送
    await send_message_in_bubbles(context.bot, chat_id, cn)


# ----------------------------------------------------------
# 推播 (修改版：使用氣泡發送)
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

    # 冷卻檢查
    if now - last_talk < 180: return
    if now - last_talk > 2 * 3600: return

    minutes_since_last = int((now - last_talk) / 60) if last_talk else None

    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=True, # 這裡設為 True 觸發 push lines
    )
    
    # 注意：這裡我們不需要額外餵 user prompt，
    # 因為 persona_config.py 裡的 timer_trigger=True 會自動把 push line 加到 system prompt 裡。
    # DeepSeek 看到 system prompt 裡的 [中斷請求] 就會自動開口。
    
    messages = [
        {"role": "system", "content": persona},
        {"role": "user", "content": "（系統自動觸發：請根據 System Prompt 中的 [中斷請求] 進行主動發言）"},
    ]

    out = await call_deepseek(messages)
    out = enforce_format_simple(out)
    cn, jp = split_reply(out)

    # 使用氣泡發送推播
    await send_message_in_bubbles(context.bot, chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


# ----------------------------------------------------------
# reset
# ----------------------------------------------------------

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)
    await update.message.reply_text("（記憶體已格式化... 我們重新開始吧。）")


# ----------------------------------------------------------
# main
# ----------------------------------------------------------

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))

    jq = getattr(app, "job_queue", None)
    if jq is not None:
        jq.run_repeating(intelligent_push, interval=1800, first=60)
        print("✅ JobQueue 啟用：已註冊 intelligent_push")
    else:
        print("⚠ JobQueue 未啟用")

    print("🚀 Lilith V9.0 — DeepSeek + Human-like Bubble Mode Running")
    app.run_polling()


if __name__ == "__main__":
    main()
