import os
import time
import asyncio
import random
import re
import logging
import requests
from datetime import datetime
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)

# ==========================================================
# ⚙️ 時區修正 (Timezone Fix)
# ==========================================================
os.environ['TZ'] = 'Asia/Taipei'
try:
    time.tzset() 
except AttributeError:
    pass 

# ==========================================================
# 📦 模組載入區 (Imports)
# ==========================================================
try:
    # ✨ 從 core 載入靈魂 (Persona)
    from core.persona_config import get_persona
    
    # 載入記憶體與新聞
    from core.redis_store import init_redis, save_history, load_history, save_state, load_state
    from core.news import search_news
    
    # 視覺模組
    try:
        from core.vision import analyze_image
    except ImportError:
        analyze_image = None
        
except ImportError as e:
    print(f"⚠️ 嚴重警告：核心模組載入失敗 ({e})。請確保 core 資料夾完整。")
    exit(1)

# ==========================================================
# ⚙️ Config & Init
# ==========================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

redis_client = init_redis(
    os.getenv("REDIS_URL"), 
    os.getenv("REDISHOST"), 
    int(os.getenv("REDISPORT", "6379")), 
    os.getenv("REDISPASSWORD")
)

# ==========================================================
# ✨ 擬人化氣泡引擎 (Visuals)
# ==========================================================
async def send_message_in_bubbles(bot, chat_id, full_text):
    if not full_text: return
    
    segments = [seg.strip() for seg in full_text.split('\n') if seg.strip()]

    for i, segment in enumerate(segments):
        delay = 0.1 + (len(segment) * 0.05)
        delay = min(delay, 2.5)
        
        if i == 0 and len(segment) < 5: 
            delay = 0.5 

        await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(delay)

        if "（" in segment and "）" in segment:
             formatted_text = re.sub(r'（(.*?)）', r'<code>（\1）</code>', segment)
        else:
             formatted_text = segment

        try:
            await bot.send_message(
                chat_id=chat_id, 
                text=formatted_text, 
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Bubble Error: {e}")

# ==========================================================
# 🧠 DeepSeek 大腦核心 (Brain Core)
# ==========================================================
async def call_deepseek(messages, length_mode="normal"):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json", 
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    
    # 📏 動態參數配置
    max_tokens_map = { "short": 150, "normal": 800, "long": 3500 }
    
    p_penalty = 0.6 if length_mode == "long" else 0.5
    f_penalty = 0.2 if length_mode == "long" else 0.2
    temp_val = 1.1 if length_mode == "long" else 1.25

    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": temp_val, 
        "max_tokens": max_tokens_map.get(length_mode, 1200), 
        "presence_penalty": p_penalty, 
        "frequency_penalty": f_penalty, 
    }

    try:
        res = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=60)
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        else:
            return f"(API Error: {res.status_code})"
    except Exception as e:
        return f"(Connection Error: {e})"

# ==========================================================
# 🧬 回覆生成邏輯 (Logic)
# ==========================================================
async def generate_reply(chat_id, user_text=None, image_b64=None, timer_trigger=False, minutes_since_last=0):
    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 1. 讀取長度設定
    length_mode = state.get("length_mode", "normal")

    # 2. 圖片處理
    if image_b64 and analyze_image:
        out = await analyze_image(image_b64)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 3. 獲取 Persona (呼叫 Core)
    # ✨ 這裡變乾淨了，直接呼叫 core 裡的函數
    persona = get_persona(
        length_mode=length_mode,  # 👈 新增這行，讓 Persona 知道現在是什麼模式
        news=state.get("news_cache", ""),
        minutes_since_last=minutes_since_last, 
        timer_trigger=timer_trigger 
    )
    # 4. 準備指令 (System Side)
    length_instruction = ""
    if length_mode == "short":
        length_instruction = "\n[SYSTEM: Keep response concise. Under 30 words.]"
    
    full_system_prompt = persona + length_instruction

    # 5. 組合訊息 (User Side Injection)
    messages = [{"role": "system", "content": full_system_prompt}] + history
    
   if user_text:
        # ✨ OOC 夾帶私貨 (Gemini 溫和版)
        final_input_text = user_text
        
        if length_mode == "long":
            # 🔴 我們不再強制格式，只提示她「多說一點心裡話」
            final_input_text += """
            
            (OOC: 請稍微多聊一點。除了回答我的問題，也說說妳作為共犯 AI 的獨特觀點或感受。就像我們在深夜談心一樣，不用拘束格式。)
            """
        
        messages.append({"role": "user", "content": final_input_text})
    # 6. 新聞搜尋觸發
    if user_text and any(k in user_text for k in ["搜尋", "查", "是誰", "新聞", "介紹"]):
        try:
            news = await search_news()
            state["news_cache"] = news 
            messages.append({"role": "system", "content": f"[搜尋結果]: {news}"})
        except: pass

    # 7. 呼叫 LLM
    out = await call_deepseek(messages, length_mode=length_mode)
    
    # 8. 更新記憶
    if user_text: history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": out})
    
    if len(history) > 40: history = history[-40:]
    
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)
    
    return out

# ==========================================================
# 🎮 指令控制台 (Commands)
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[系統] 喚醒莉莉絲"""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID: return
    await update.message.reply_text("⚡ 系統初始化中... 連接神經網路... 莉莉絲已上線。")
    out = await generate_reply(chat_id, user_text="(System: User started the bot. Say hello casually.)")
    await send_message_in_bubbles(context.bot, chat_id, out)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    help_text = (
        "<b>🔰 莉莉絲控制終端</b>\n"
        "--------------------------------\n"
        "<code>/reset</code> - 重置記憶\n"
        "<code>/len [short|normal|long]</code> - 設定長度\n"
        "<code>/news [關鍵字]</code> - 搜尋新聞\n"
        "<code>/care</code> - 測試主動關心\n"
        "<code>/status</code> - 查看狀態\n"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.HTML)

async def cmd_set_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    args = context.args
    if not args or args[0] not in ["short", "normal", "long"]:
        await update.message.reply_text("⚠️ 用法: /len short | normal | long")
        return
    mode = args[0]
    chat_id = update.effective_chat.id
    state = load_state(chat_id, redis_client)
    state["length_mode"] = mode
    save_state(chat_id, state, redis_client)
    msg_map = {
        "short": "（⚡ 切換模式：簡潔。省話一姐上線。）",
        "normal": "（✨ 切換模式：標準。恢復正常節奏。）",
        "long": "（📝 切換模式：長文。準備開始寫作文囉。）"
    }
    await update.message.reply_text(msg_map[mode])

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    chat_id = update.effective_chat.id
    query = " ".join(context.args) if context.args else "科技新聞"
    await update.message.reply_text(f"🔍 搜尋中：{query}...")
    try:
        try: news_result = await search_news(query)
        except TypeError: news_result = await search_news()
        state = load_state(chat_id, redis_client)
        state["news_cache"] = news_result
        save_state(chat_id, state, redis_client)
        out = await generate_reply(chat_id, user_text=f"(System Action: User executed search for '{query}'. Research Result: {news_result}. Summarize and comment.)")
        await send_message_in_bubbles(context.bot, chat_id, out)
    except Exception as e:
        await update.message.reply_text(f"❌ 搜尋失敗: {e}")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    save_history(ADMIN_ID, [], redis_client)
    state = {"last_user_timestamp": time.time(), "has_sent_care": False, "length_mode": "normal"}
    save_state(ADMIN_ID, state, redis_client)
    await update.message.reply_text("🗑️ 記憶已重置。")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    state = load_state(ADMIN_ID, redis_client)
    last_ts = state.get("last_user_timestamp", 0)
    mode = state.get("length_mode", "normal")
    minutes = int((time.time() - last_ts) / 60) if last_ts else 0
    now = datetime.now().strftime("%H:%M")
    status_text = (
        f"🏥 <b>LILITH SYSTEM STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕒 當前時間: <code>{now}</code>\n"
        f"⏱️ 沉默時間: <code>{minutes} min</code>\n"
        f"📏 回覆長度: <code>{mode}</code>\n"
        f"💤 關心鎖定: <code>{state.get('has_sent_care', False)}</code>\n"
    )
    await update.message.reply_text(status_text, parse_mode=constants.ParseMode.HTML)

async def cmd_force_care(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    await update.message.reply_text("🧪 強制注入孤獨感參數...")
    out = await generate_reply(ADMIN_ID, user_text="(System Test: Force Trigger Care)", timer_trigger=True, minutes_since_last=300)
    await send_message_in_bubbles(context.bot, ADMIN_ID, out)

# ==========================================================
# ❤️ 主動關心與訊息處理 (Handlers & Jobs)
# ==========================================================
async def check_inactivity_and_care(context: ContextTypes.DEFAULT_TYPE):
    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    last_ts = state.get("last_user_timestamp", 0)
    now_ts = time.time()
    minutes_since_last = int((now_ts - last_ts) / 60)
    current_hour = datetime.now().hour
    is_sleeping_time = (2 <= current_hour < 8)
    has_sent_care = state.get("has_sent_care", False)

    if minutes_since_last >= 240 and not is_sleeping_time and not has_sent_care:
        logging.info("💗 觸發主動關心機制！")
        out = await generate_reply(chat_id, user_text="(System: User 消失超過 4 小時，請主動探頭關心)", timer_trigger=True, minutes_since_last=minutes_since_last)
        await send_message_in_bubbles(context.bot, chat_id, out)
        state["has_sent_care"] = True
        save_state(chat_id, state, redis_client)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    chat_id = update.effective_chat.id
    text = update.message.text
    image_b64 = None
    state = load_state(chat_id, redis_client)
    state["last_user_timestamp"] = time.time()
    state["has_sent_care"] = False 
    save_state(chat_id, state, redis_client)
    out = await generate_reply(chat_id, user_text=text, image_b64=image_b64)
    await send_message_in_bubbles(context.bot, chat_id, out)

# ==========================================================
# 🚀 啟動區 (Boot)
# ==========================================================
def main():
    print("🚀 Lilith v10.0 (Refactored Core) is waking up...")
    time.sleep(5) 

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # 註冊指令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("len", cmd_set_length))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("care", cmd_force_care))
    app.add_handler(CommandHandler("status", cmd_status))
    
    # 註冊訊息處理
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    # 啟用心跳機制
    if app.job_queue:
        app.job_queue.run_repeating(check_inactivity_and_care, interval=600, first=60)
        print("✅ 生命維持系統 (Heartbeat) 已連線。")

    print("🏥 System Ready. Connection established.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


