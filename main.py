import os, asyncio, random, time, re, difflib
from datetime import datetime
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

# 載入你的核心模組 (確保這些檔案都在 core 資料夾下)
from core.persona_config import get_persona
from core.redis_store import init_redis, save_history, load_history, save_state, load_state
from core.news import search_news
# 如果你還沒寫好 vision，這行可以先註解掉，不然會報錯
try:
    from core.vision import analyze_image
except ImportError:
    analyze_image = None

# ----------------------------------------------------------
# ENV 設定
# ----------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 初始化 Redis
redis_client = init_redis(
    os.getenv("REDIS_URL"), 
    os.getenv("REDISHOST"), 
    int(os.getenv("REDISPORT", "6379")), 
    os.getenv("REDISPASSWORD")
)

# ==========================================================
# ✨ 擬人化氣泡引擎 (您的得意之作，保留！)
# ==========================================================
async def send_message_in_bubbles(bot, chat_id, full_text):
    if not full_text: return
    segments = [seg.strip() for seg in full_text.split('\n') if seg.strip()]

    for i, segment in enumerate(segments):
        # 模擬打字延遲：基礎 0.5s + 每個字 0.05s
        delay = 0.5 + (len(segment) * 0.05)
        delay = min(delay, 3.0) # 上限 3 秒
        if i == 0 and len(segment) < 5: delay = 0.5 # 短句秒回

        # 顯示 "正在輸入..."
        await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(delay)

        # 將 （動作） 轉為 Code Style
        if "（" in segment and "）" in segment:
             formatted_text = re.sub(r'（(.*?)）', r'<code>（\1）</code>', segment)
        else:
             formatted_text = segment

        try:
            await bot.send_message(chat_id=chat_id, text=formatted_text, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            print(f"Send error: {e}")

# ==========================================================
# 🧠 大腦與生成邏輯
# ==========================================================
async def call_deepseek(messages):
    import requests
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 1.2, # 稍微調高溫度，讓蛋糕更甜更隨機
        "max_tokens": 500,
    }
    try:
        res = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=30)
        return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"(連線有點不穩定... 是因為我想你了嗎？ {e})"

async def generate_reply(chat_id, user_text=None, image_b64=None, timer_trigger=False, minutes_since_last=0):
    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 如果有圖片 (且 Vision 模組存在)
    if image_b64 and analyze_image:
        out = await analyze_image(image_b64)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 獲取 Persona (這裡會把時間參數傳進去，讓蛋糕決定要不要撒嬌)
    persona = get_persona(
        news=state.get("news_cache", ""),
        minutes_since_last=minutes_since_last, # 傳入時間
        timer_trigger=timer_trigger # 告訴她是不是鬧鐘叫醒的
    )

    messages = [{"role": "system", "content": persona}] + history
    if user_text:
        messages.append({"role": "user", "content": user_text})

    # 執行新聞搜尋 (僅在用戶主動詢問時)
    if user_text and any(k in user_text for k in ["搜尋", "查", "是誰", "新聞"]):
        try:
            news = await search_news()
            state["news_cache"] = news # 更新緩存
            messages.append({"role": "system", "content": f"[System] 搜尋結果: {news}"})
        except: pass

    # 呼叫 LLM
    out = await call_deepseek(messages)
    
    # 儲存對話
    if user_text: history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": out})
    # 保持記憶長度 (例如只留最後 20 句)
    if len(history) > 20: history = history[-20:]
    
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client) # 保存狀態
    return out

# ==========================================================
# ❤️ 主動關心機制 (Heartbeat Check) - 這裡大改了！
# ==========================================================
async def check_inactivity_and_care(context: ContextTypes.DEFAULT_TYPE):
    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    
    last_ts = state.get("last_user_timestamp", 0)
    now_ts = time.time()
    
    # 計算消失分鐘數
    minutes_since_last = int((now_ts - last_ts) / 60)
    
    # 取得現在幾點 (避免半夜 3 點吵你)
    current_hour = datetime.now().hour
    is_sleeping_time = (2 <= current_hour < 8)

    # 判斷是否已經關心過了 (防止每 10 分鐘炸一次)
    has_sent_care = state.get("has_sent_care", False)

    # 觸發條件：
    # 1. 消失超過 4 小時 (240分鐘)
    # 2. 不是睡覺時間
    # 3. 還沒發送過關心訊息
    if minutes_since_last >= 240 and not is_sleeping_time and not has_sent_care:
        
        # 觸發！
        out = await generate_reply(
            chat_id, 
            user_text="(System: User 消失很久了，請探頭進來關心他)", 
            timer_trigger=True,  # ★ 關鍵：開啟主動模式
            minutes_since_last=minutes_since_last
        )
        
        # 發送
        await send_message_in_bubbles(context.bot, chat_id, out)
        
        # 標記已發送，避免重複
        state["has_sent_care"] = True
        save_state(chat_id, state, redis_client)

# ==========================================================
# 🎮 Handlers
# ==========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    
    text = update.message.text
    chat_id = update.effective_chat.id
    
    # ★ 只要你說話了，就更新時間戳，並重置「已關心」標記
    state = load_state(chat_id, redis_client)
    state["last_user_timestamp"] = time.time()
    state["has_sent_care"] = False  # 重置標記，下次還可以再關心
    save_state(chat_id, state, redis_client)

    # 處理圖片 (如果有)
    image_b64 = None
    if update.message.photo:
        # 這裡簡化處理，實際需要下載圖片轉 base64
        # image_b64 = await download_and_encode(update.message.photo[-1])
        pass 

    out = await generate_reply(chat_id, user_text=text, image_b64=image_b64)
    
    # 分割中日文 (選用)
    if "|||" in out: out = out.split("|||")[0]
    
    await send_message_in_bubbles(context.bot, chat_id, out)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_history(ADMIN_ID, [], redis_client)
    state = load_state(ADMIN_ID, redis_client)
    state["last_user_timestamp"] = time.time() # 重置也要更新時間，不然馬上被觸發關心
    save_state(ADMIN_ID, state, redis_client)
    await update.message.reply_text("（記憶體已格式化... 莉莉絲重新載入中。）")

# ==========================================================
# 🚀 啟動區
# ==========================================================
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # 支援文字和圖片
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # 啟用心跳檢查 (每 10 分鐘檢查一次)
    if app.job_queue:
        app.job_queue.run_repeating(check_inactivity_and_care, interval=600, first=60)
        print("✅ 莉莉絲心跳機制已啟動：每 10 分鐘檢查你是否還活著。")

    print("🚀 Lilith v9.1 Active - The Strawberry Cake Edition")
    app.run_polling()

if __name__ == "__main__":
    main()
