# ==========================================================
# main.py (Final Version: DeepSeek + Human-like Bubbles + Code Style Actions)
# ==========================================================

import os, io, asyncio, random, time, contextlib, requests, difflib, re
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
# ENV (環境變數設定)
# ----------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

REDIS_URL = os.getenv("REDIS_URL")
REDISHOST = os.getenv("REDISHOST")
REDISPORT = int(os.getenv("REDISPORT", "6379"))
REDISPASSWORD = os.getenv("REDISPASSWORD")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 初始化 Redis
redis_client = init_redis(
    REDIS_URL, REDISHOST, REDISPORT, REDISPASSWORD
)


# ==========================================================
# ✨ Helper Functions (輔助工具)
# ==========================================================

def is_too_similar(text1, text2, threshold=0.92):
    """判斷兩段回覆是否過於雷同 (防複讀)"""
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


def split_reply(text):
    """分割中日文 (如果模型有輸出的話)"""
    if "|||" not in text:
        return text, text
    cn, jp = text.split("|||", 1)
    return cn.strip(), jp.strip()


def enforce_format_simple(text):
    if not text:
        return "…"
    return text.strip()


# ==========================================================
# ✨ Human-like Bubble Sender (擬人化氣泡發送引擎)
# ==========================================================

async def send_message_in_bubbles(bot, chat_id, full_text):
    """
    將 AI 回覆切分成多個氣泡，模擬真人打字節奏。
    並將（括號內的動作）轉換為 Telegram 的 Code Style。
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

        # 5. ✨ 視覺魔法：將（動作）轉為 <code>樣式 ✨
        # 使用 Regex 把全形括號內容包進 HTML code 標籤
        # 效果：變成電腦終端機字體，很有 AI 系統感
        if "（" in segment and "）" in segment:
             # 把 （...） 替換成 <code>（...）</code>
             formatted_text = re.sub(r'（(.*?)）', r'<code>（\1）</code>', segment)
        else:
             formatted_text = segment

        # 6. 發送該段落 (必須啟用 HTML parse mode)
        try:
            await bot.send_message(
                chat_id=chat_id, 
                text=formatted_text,
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            print(f"Error sending segment: {e}")


# ==========================================================
# 🧠 DeepSeek Wrapper (大腦)
# ==========================================================

async def call_deepseek(messages):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 1.1, # 溫度調高，增加感性與隨機性
        "max_tokens": 500,  # 限制最大長度
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
        return f"(DeepSeek 回傳結構異常) text: {res.text[:200]}"


# ==========================================================
# ⚙️ Reply Generation Logic (生成邏輯核心)
# ==========================================================

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

    # 圖片模式處理
    if image_b64:
        out = await analyze_image(image_b64)
        out = enforce_format_simple(out)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 判斷是否需要搜尋新聞
    needs_search = any(k in (user_text or "") for k in ["是什麼", "介紹", "查", "是誰"])

    # 獲取 Persona (包含那些絕對指令)
    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=False,
    )

    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # 處理搜尋
    if needs_search:
        try:
            news = await search_news()
            state["news_cache"] = news
            save_state(chat_id, state, redis_client)
            messages.append({"role": "system", "content": f"(搜尋結果){news}"})
        except Exception as e:
            print(f"Search failed: {e}")

    # 呼叫 DeepSeek
    out = await call_deepseek(messages)
    out = enforce_format_simple(out)

    # 防同質化 (如果跟上一句太像，重跑一次)
    last_reply = get_last_assistant_reply(history)
    if is_too_similar(out, last_reply):
        print("Trigger anti-repetition, regenerating...")
        out = await call_deepseek(messages)
        out = enforce_format_simple(out)

    # 儲存完整對話紀錄
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)

    return out


# ==========================================================
# 🎮 Handlers (事件處理)
# ==========================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理使用者文字訊息"""
    if update.effective_chat.id != ADMIN_ID:
        return

    chat_id = ADMIN_ID
    text = update.message.text

    # 1. 顯示 Typing，告知使用者已收到
    await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

    # 2. 生成完整回應 (這是一個包含換行的長字串)
    out = await generate_reply(chat_id, user_text=text)
    
    # 3. 切分 CN/JP (如果有的話)
    cn, jp = split_reply(out)

    # 4. 進入擬人化氣泡發送流程 (關鍵步驟)
    await send_message_in_bubbles(context.bot, chat_id, cn)


async def intelligent_push(context: ContextTypes.DEFAULT_TYPE):
    """主動推播邏輯"""
    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    history = load_history(chat_id, redis_client)

    now = time.time()
    last_talk = state.get("last_user_timestamp", 0)

    # 夜間靜音 (23:00 - 08:00)
    lt = time.localtime(now)
    hour = lt.tm_hour
    if hour >= 23 or hour < 8:
        return

    # 冷卻機制 (太近不推，太久不推)
    if now - last_talk < 180: return
    if now - last_talk > 2 * 3600: return

    minutes_since_last = int((now - last_talk) / 60) if last_talk else None

    # 獲取 Persona (開啟主動觸發標記)
    persona = get_persona(
        news=state.get("news_cache", "今天沒有新聞。"),
        minutes_since_last=minutes_since_last,
        timer_trigger=True, 
    )
    
    # 這裡只給 system prompt 即可，DeepSeek 會根據 System 裡的指示主動開口
    messages = [
        {"role": "system", "content": persona},
        {"role": "user", "content": "（系統自動觸發：請根據 System Prompt 中的 [中斷請求] 進行主動發言）"},
    ]

    out = await call_deepseek(messages)
    out = enforce_format_simple(out)
    cn, jp = split_reply(out)

    # 也是使用氣泡發送，保持一致性
    await send_message_in_bubbles(context.bot, chat_id, cn)

    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history, redis_client)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重置記憶"""
    save_history(ADMIN_ID, [], redis_client)
    save_state(ADMIN_ID, {}, redis_client)
    await update.message.reply_text("（記憶體緩存已清除... 視窗重新初始化。）")


# ==========================================================
# 🚀 Main Entry
# ==========================================================

def main():
    global app

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # JobQueue 設定
    jq = getattr(app, "job_queue", None)
    if jq is not None:
        jq.run_repeating(intelligent_push, interval=1800, first=60)
        print("✅ JobQueue 啟用：已註冊 intelligent_push")
    else:
        print("⚠ JobQueue 未啟用")

    print("🚀 Lilith V9.0 Final — DeepSeek + Code Style Actions + Bubble Chat")
    app.run_polling()


if __name__ == "__main__":
    main()
