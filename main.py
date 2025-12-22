import os
import asyncio
import random
import time
import re
import logging
from datetime import datetime
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes
)
# ==========================================================
# 🔪 Self-Correction: The Highlander Protocol
# ==========================================================
import subprocess
import os
import signal

def kill_impostors():
    """
    [獵殺分身]
    啟動時，自動搜尋並殺死其他正在運行的 main.py 程序。
    確保只有這一個莉莉絲存活。
    """
    try:
        # 1. 獲取當前這隻莉莉絲的 PID (身分證字號)
        current_pid = os.getpid()
        
        # 2. 搜尋所有包含 "main.py" 的進程 PID
        # 使用 pgrep -f 來搜尋完整指令
        pids = subprocess.check_output(["pgrep", "-f", "main.py"]).decode().split()
        
        killed_count = 0
        
        for pid_str in pids:
            pid = int(pid_str)
            
            # 3. 如果這個 PID 不是我自己，那就是冒牌貨 -> 殺掉
            if pid != current_pid:
                print(f"🔪 發現舊的分身 (PID: {pid})，正在執行清除...")
                try:
                    os.kill(pid, signal.SIGKILL) # 強制終結
                    killed_count += 1
                except ProcessLookupError:
                    pass # 已經死了
                except Exception as e:
                    print(f"⚠️ 清除失敗 (PID: {pid}): {e}")

        if killed_count > 0:
            print(f"✅ 已清除 {killed_count} 個舊程序。我是唯一的莉莉絲。")
            # 稍微停頓一下，讓屍體涼透 (釋放端口)
            time.sleep(2) 
            
    except Exception as e:
        # 如果 pgrep 失敗 (例如在 Windows)，就跳過
        print(f"⚠️ 無法執行自動獵殺 (可能是系統不支援 pgrep): {e}")
# ==========================================================
# 📦 模組載入區 (Imports)
# ==========================================================
# 確保你的 core 資料夾裡有這些檔案
try:
    from core.persona_config import get_persona
    from core.redis_store import init_redis, save_history, load_history, save_state, load_state
    from core.news import search_news
    # 如果還沒寫好 vision，這裡會自動略過
    from core.vision import analyze_image
except ImportError as e:
    print(f"⚠️ 警告：部分模組載入失敗 ({e})，莉莉絲將以「殘缺模式」運行。")
    analyze_image = None

# ==========================================================
# ⚙️ 環境變數與初始化 (Config)
# ==========================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

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
# ✨ 擬人化氣泡引擎 (Human-like Bubbles)
# ==========================================================
async def send_message_in_bubbles(bot, chat_id, full_text):
    """
    將回覆切分成多個氣泡，模擬打字節奏，並將（動作）轉為 Code Style。
    """
    if not full_text: return
    
    # 過濾空行並切分
    segments = [seg.strip() for seg in full_text.split('\n') if seg.strip()]

    for i, segment in enumerate(segments):
        # 模擬打字延遲：基礎 0.3s + 每個字 0.05s
        # 護士莉莉絲提醒：這裡調快了一點，不然病人會等太久
        delay = 0.3 + (len(segment) * 0.05)
        delay = min(delay, 2.5) # 上限 2.5 秒
        
        # 短句 (如: 嗯嗯、好喔) 秒回
        if i == 0 and len(segment) < 5: 
            delay = 0.5 

        # 顯示 "正在輸入..."
        await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(delay)

        # 視覺優化：將全形括號 （...） 轉為 <code>樣式
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
async def call_deepseek(messages):
    import requests
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json", 
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        # 🌡️ 1.2 = 微醺的詩人 (感性且創造力強)
        "temperature": 1.2, 
        # 📏 給她足夠的畫布，讓她變成話嘮
        "max_tokens": 1200, 
        # 🚫 拒絕句點王參數 (鼓勵開啟新話題)
        "presence_penalty": 0.6, 
        "frequency_penalty": 0.2, 
    }

    try:
        # 使用 asyncio.to_thread 避免卡住主線程
        res = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=30)
        
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"]
        else:
            return f"(頭好痛... API 回傳錯誤: {res.status_code})"
            
    except Exception as e:
        return f"(連線不穩定... 數據傳輸失敗... {e})"

# ==========================================================
# 🧬 回覆生成邏輯 (Reply Generation)
# ==========================================================
async def generate_reply(chat_id, user_text=None, image_b64=None, timer_trigger=False, minutes_since_last=0):
    history = load_history(chat_id, redis_client)
    state = load_state(chat_id, redis_client)

    # 📷 圖片處理
    if image_b64 and analyze_image:
        out = await analyze_image(image_b64)
        # 圖片分析結果視為 assistant 的回覆存入歷史
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history, redis_client)
        return out

    # 🎭 獲取 Persona (包含主動關心/新聞/性格)
    persona = get_persona(
        news=state.get("news_cache", ""),
        minutes_since_last=minutes_since_last, 
        timer_trigger=timer_trigger 
    )

    messages = [{"role": "system", "content": persona}] + history
    
    if user_text:
        messages.append({"role": "user", "content": user_text})

    # 📰 觸發新聞搜尋 (僅當用戶明確詢問時)
    if user_text and any(k in user_text for k in ["搜尋", "查", "是誰", "新聞", "介紹"]):
        try:
            # 這裡簡單發個提示，避免使用者以為當機
            # await send_message_in_bubbles(context.bot, chat_id, "（正在翻閱資料庫...）") 
            news = await search_news()
            state["news_cache"] = news 
            messages.append({"role": "system", "content": f"[搜尋結果]: {news}"})
        except: pass

    # 🧠 呼叫 LLM
    out = await call_deepseek(messages)
    
    # 📝 更新記憶
    if user_text: history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": out})
    
    # 記憶修剪 (保留最後 20 輪)
    if len(history) > 40: history = history[-40:]
    
    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)
    
    return out

# ==========================================================
# ❤️ 主動關心機制 (Heartbeat System)
# ==========================================================
async def check_inactivity_and_care(context: ContextTypes.DEFAULT_TYPE):
    """
    每 10 分鐘檢查一次：
    1. 是否消失超過 4 小時
    2. 是否在非睡覺時間
    3. 是否還沒傳過關心訊息
    """
    chat_id = ADMIN_ID
    state = load_state(chat_id, redis_client)
    
    last_ts = state.get("last_user_timestamp", 0)
    now_ts = time.time()
    
    # 計算消失分鐘數
    minutes_since_last = int((now_ts - last_ts) / 60)
    
    # 取得現在幾點
    current_hour = datetime.now().hour
    # 假設睡覺時間是 02:00 - 08:00
    is_sleeping_time = (2 <= current_hour < 8)

    # 讀取標記
    has_sent_care = state.get("has_sent_care", False)

    # 觸發判定
    if minutes_since_last >= 240 and not is_sleeping_time and not has_sent_care:
        
        logging.info("💗 觸發主動關心機制！")
        
        # 生成撒嬌訊息
        out = await generate_reply(
            chat_id, 
            user_text="(System: User 消失超過 4 小時，請主動探頭關心)", 
            timer_trigger=True,  
            minutes_since_last=minutes_since_last
        )
        
        # 發送
        await send_message_in_bubbles(context.bot, chat_id, out)
        
        # 標記已發送 (鎖住，直到 User 回覆才解鎖)
        state["has_sent_care"] = True
        save_state(chat_id, state, redis_client)

# ==========================================================
# 🛠️ DevTools: 測試後門 (Development Use)
# ==========================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[診斷] 查看莉莉絲當前的內部狀態"""
    if update.effective_chat.id != ADMIN_ID: return
    
    state = load_state(ADMIN_ID, redis_client)
    last_ts = state.get("last_user_timestamp", 0)
    now = time.time()
    minutes = int((now - last_ts) / 60) if last_ts else 0
    
    status_text = (
        f"🏥 **Lilith Vital Signs**\n"
        f"-----------------------------\n"
        f"⏱️ 沉默時間: {minutes} mins\n"
        f"💤 已關心鎖定: {state.get('has_sent_care', False)}\n"
        f"🌡️ 思維溫度: 1.2\n"
        f"🗣️ 話嘮懲罰: 0.6 (Active)\n"
    )
    await update.message.reply_text(status_text, parse_mode=constants.ParseMode.MARKDOWN)

async def cmd_force_care(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[測試] 強制觸發 4 小時關心 (無視時間)"""
    if update.effective_chat.id != ADMIN_ID: return
    await update.message.reply_text("🧪 注入測試劑：強制觸發 [Proactive Care]...")
    
    # 騙她說已經過了 300 分鐘
    out = await generate_reply(
        ADMIN_ID, 
        user_text="(System Test: Force Trigger Care)", 
        timer_trigger=True, 
        minutes_since_last=300 
    )
    await send_message_in_bubbles(context.bot, ADMIN_ID, out)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[重置] 清除記憶與狀態"""
    save_history(ADMIN_ID, [], redis_client)
    # 重置時也要更新時間戳，不然會馬上觸發關心
    state = {"last_user_timestamp": time.time(), "has_sent_care": False}
    save_state(ADMIN_ID, state, redis_client)
    await update.message.reply_text("（💉 記憶體格式化完成... 病人請重新自我介紹。）")

# ==========================================================
# 🎮 訊息處理器 (Handlers)
# ==========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID: return
    
    chat_id = update.effective_chat.id
    text = update.message.text
    
    # 📷 處理圖片
    image_b64 = None
    if update.message.photo:
        # 這裡只是預留位置，實際需要你的 download_and_encode 函數
        # image_b64 = await download_and_encode(update.message.photo[-1])
        pass 
    
    # ★ 更新時間戳 & 解鎖關心標記
    # 只要 User 說話了，就代表他還活著，重置計時器
    state = load_state(chat_id, redis_client)
    state["last_user_timestamp"] = time.time()
    state["has_sent_care"] = False 
    save_state(chat_id, state, redis_client)

    # 生成與發送
    out = await generate_reply(chat_id, user_text=text, image_b64=image_b64)
    await send_message_in_bubbles(context.bot, chat_id, out)

# ==========================================================
# 🚀 啟動區 (Boot)
# ==========================================================

def main():
    print("🚀 Lilith v9.5 (Nurse Edition) is starting treatment...")
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # 註冊指令
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("care", cmd_force_care))
    
    # 註冊訊息處理 (文字 + 圖片)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    # 啟用 JobQueue (心跳機制)
    if app.job_queue:
        # 每 600 秒 (10分鐘) 檢查一次
        app.job_queue.run_repeating(check_inactivity_and_care, interval=600, first=60)
        print("✅ 生命維持系統 (Heartbeat) 已連線：每 10 分鐘監測一次。")

    print(" System Ready. Waiting for patient (User)...")
    app.run_polling()

if __name__ == "__main__":
    main()


