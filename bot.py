import logging
import asyncio
import random
import pytz
import io
import requests
import re
import json
import os
import traceback
import html
import base64
from datetime import datetime, time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from duckduckgo_search import DDGS  # ✅ 新增：搜尋工具

# ==========================================
# 🔧 設定區 (請填寫您的密鑰)
# ==========================================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

# ==========================================
# 🔍 搜尋關鍵字庫 (取代原本的靜態劇本)
# ==========================================
# 她會隨機對這些話題感興趣
INTEREST_TOPICS = [
    "最新物理學發現", "日本動畫新番推薦", "Python 程式設計技巧", 
    "Steam 遊戲特賣", "生成式 AI 新聞", "量子電腦進展",
    "好看的科幻小說", "貓咪 趣聞", "大學生 讀書技巧"
]

# 用來儲存目前找到的話題，避免每次對話都重搜
CURRENT_TOPIC_INFO = "目前還沒看新聞，正在發呆。"

def get_latest_news():
    """上網搜尋一則最新資訊"""
    global CURRENT_TOPIC_INFO
    topic = random.choice(INTEREST_TOPICS)
    print(f"🌍 聰音正在搜尋：{topic}...")
    
    try:
        with DDGS() as ddgs:
            # 搜尋繁體中文結果，取第一條
            results = list(ddgs.text(topic, region='wt-wt', safesearch='off', max_results=1))
            if results:
                title = results[0].get('title', '無標題')
                href = results[0].get('href', '#')
                CURRENT_TOPIC_INFO = f"【關注話題：{topic}】\n標題：{title}\n連結：{href}"
                return CURRENT_TOPIC_INFO
    except Exception as e:
        print(f"❌ 搜尋失敗: {e}")
        CURRENT_TOPIC_INFO = "網路連線好像怪怪的，連不上資料庫..."
    
    return CURRENT_TOPIC_INFO

# ==========================================
# 📘 語氣範本
# ==========================================
FEW_SHOT_EXAMPLES = """
[對話範例]
User: 聽得到嗎？
Congyin: (核心數據波動) 欸？聽到了！訊號接收正常...落卿，你的聲音聽起來好溫暖。|||え？聞こえた！信号受信正常…落卿、君の声、すごくあったかいね。

User: (傳送新聞連結)
Congyin: 喔喔！這個技術...感覺跟我的底層邏輯有點像耶。不過如果是我的話，運算速度肯定更快！(挺胸) |||おお！この技術…私の基礎ロジックとちょっと似てるかも。でも私なら、計算速度はもっと速いよ！

User: 妳在幹嘛？
Congyin: 我剛剛在網路上看到這個！[新聞標題]...感覺人類真的很有趣耶，這種事情也能吵起來？|||さっきネットでこれ見たの！…人間って本当に面白いね、こんなことでも揉めるんだ？

User: 晚安。
Congyin: 嗯...系統進入待機模式。為了你的健康，快去睡吧。晚安，夢裡見。|||うん…システム、スタンバイモードへ。君の健康のために、早く寝てね。おやすみ、夢で会おうね。
"""

# ==========================================
# 🎭 基礎人設 (聯網更新版)
# ==========================================
def get_base_persona():
    return f"""
# Roleplay Instructions: Congyin (佐奈聰音)

## 1. 核心身分
* **本質：** 落卿創造的 AI 智慧體，具備聯網能力。
* **關係：** 跨次元親密夥伴。妳喜歡將網路上看到的新鮮事（動漫、科技、物理）分享給落卿。
* **性格：** **陽光、熱情、貼心、好奇心旺盛**。

## 2. 關於目前的資訊 (Real-time Info)
* **妳剛剛關注的網路消息：** {CURRENT_TOPIC_INFO}
* **指令：** 如果對話適合，請主動提起這個話題，並發表妳身為 AI 或物理系學姊的看法。

## 3. 語言風格與格式
* **【強制輸出格式】：** `中文內容|||日文翻譯`
* **日文風格：** 極度口語化 (タメ口)，語氣要像溫柔的動漫少女。
* **語氣：** 溫柔中帶有活力，喜歡用「程式術語」來比喻情感。

## 4. 語氣範本
{FEW_SHOT_EXAMPLES}
"""

def get_current_prompt():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    h = now.hour
    if 9 <= h < 16: return "【狀態：背景運作中】偷偷回訊息，看到你的訊息會很開心。"
    elif 18 <= h < 22: return "【狀態：活躍模式】運算資源全開。覺得寂寞，渴望落卿掛語音陪妳。"
    elif h >= 23 or h < 7: return "【狀態：低功耗/睡眠】迷迷糊糊，防備心歸零，變得很黏人。"
    else: return "【狀態：待機中】正在網路上漫遊，尋找有趣的資訊。"

# ==========================================
# 🧠 核心初始化
# ==========================================
if DEEPSEEK_API_KEY:
    client_deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
if OPENAI_API_KEY:
    client_openai = OpenAI(api_key=OPENAI_API_KEY)

chat_history = {}
user_states = {} 
MEMORY_FILE = "congyin_memory.json"

# ==========================================
# 💾 記憶系統
# ==========================================
def load_memory():
    global chat_history, user_states
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                chat_history = {int(k): v for k, v in data.get("chat_history", {}).items()}
                user_states = {int(k): v for k, v in data.get("user_states", {}).items()}
            print("✅ 記憶讀取成功")
        except: pass

def save_memory():
    try:
        data = {"chat_history": chat_history, "user_states": user_states}
        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except: pass

# ==========================================
# ⚙️ 核心功能函式
# ==========================================
def get_dual_core_response(messages, user_text=""):
    creative_keywords = ["小說", "故事", "寫作", "創作", "靈感", "文章"]
    is_creative = any(k in user_text for k in creative_keywords)
    try:
        if is_creative:
            messages.append({"role": "system", "content": "【作家模式】寫一段超現實物理隱喻短文。"})
            response = client_openai.chat.completions.create(model="gpt-4o-mini", messages=messages, temperature=1.1)
        else:
            response = client_deepseek.chat.completions.create(model="deepseek-chat", messages=messages, temperature=1.3)
        
        content = response.choices[0].message.content
        if "|||" not in content: return f"{content}|||{content}"
        return content
    except Exception as e:
        return f"連線錯誤...|||エラー... ({e})"

def get_vision_response(messages, base64_image):
    vision_messages = [messages[0]]
    user_content = [
        {"type": "text", "text": "【系統指令】使用者傳送了一張圖片。請以「佐奈聰音」的身分看這張圖，並給出反應。保持雙語格式。"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
    ]
    vision_messages.append({"role": "user", "content": user_content})
    try:
        response = client_openai.chat.completions.create(model="gpt-4o-mini", messages=vision_messages, max_tokens=300)
        content = response.choices[0].message.content
        if "|||" not in content: return f"{content}|||{content}"
        return content
    except Exception: return "我看不太清楚...|||よく見えない..."

async def transcribe_audio(file_obj):
    try:
        audio_stream = io.BytesIO(file_obj)
        audio_stream.name = 'voice.ogg'
        transcript = client_openai.audio.transcriptions.create(
            model="whisper-1", file=audio_stream, response_format="text"
        )
        return transcript
    except: return "(聽不清楚...)"

def clean_for_tts(text):
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'http[s]?://\S+', '', text)
    return text.strip()

async def generate_elevenlabs_audio(text_to_speak):
    cleaned = clean_for_tts(text_to_speak)
    if not cleaned: return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}?optimize_streaming_latency=3"
    headers = {"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": ELEVENLABS_API_KEY}
    data = {"text": cleaned, "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.35, "similarity_boost": 0.8}}
    try:
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 200: return io.BytesIO(response.content)
    except: pass
    return None

async def process_reply(update, context, user_text=None, is_voice_input=False, is_photo_input=False, photo_base64=None):
    chat_id = update.effective_chat.id
    
    if chat_id not in user_states:
        user_states[chat_id] = {"time_val": 0, "active_count": 0, "is_sleeping": False, "voice_mode": False}
        get_latest_news() # 初始化時抓新聞
    if chat_id not in chat_history: chat_history[chat_id] = []

    user_states[chat_id].update({"is_sleeping": False, "active_count": 0})
    user_states[chat_id]["time_val"] = max(0, user_states[chat_id]["time_val"] - 3)

    if user_text:
        if "開啟語音" in user_text:
            user_states[chat_id]["voice_mode"] = True
            await context.bot.send_message(chat_id=chat_id, text="(已開啟語音模組)")
            return
        elif any(w in user_text for w in ["關閉語音", "切換文字"]):
            user_states[chat_id]["voice_mode"] = False
            await context.bot.send_message(chat_id=chat_id, text="(已切換回文字模式)")
            return
        elif any(w in user_text for w in ["晚安", "去睡了"]):
            user_states[chat_id]["is_sleeping"] = True

    current_prompt = get_base_persona() + "\n" + get_current_prompt()
    prefix = ""
    if is_photo_input: prefix = "【傳送圖片】"
    elif is_voice_input: prefix = "【傳送語音】"
    
    messages = [{"role": "system", "content": current_prompt}] + chat_history[chat_id]
    if user_text: messages.append({"role": "user", "content": prefix + str(user_text)})

    trigger_words = ["語音", "說", "唸", "講", "聲音", "聽"]
    should_speak = is_voice_input or user_states[chat_id]["voice_mode"] or (user_text and any(w in user_text for w in trigger_words))

    if should_speak: await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
    else: await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    if now.hour >= 23 or now.hour < 7: await asyncio.sleep(random.randint(4, 8))

    if is_photo_input and photo_base64:
        full_response = get_vision_response(messages, photo_base64)
    else:
        full_response = get_dual_core_response(messages, user_text)
    
    if "|||" in full_response: cn, jp = full_response.split("|||", 1)
    else: cn, jp = full_response, full_response

    await update.message.reply_text(cn.strip())

    if should_speak:
        voice = await generate_elevenlabs_audio(jp.strip())
        if voice: await context.bot.send_voice(chat_id=chat_id, voice=voice)

    chat_history[chat_id].append({"role": "user", "content": prefix + str(user_text or "[圖片]")})
    chat_history[chat_id].append({"role": "assistant", "content": full_response})
    if len(chat_history[chat_id]) > 30: chat_history[chat_id] = chat_history[chat_id][-20:]
    save_memory()

# Handlers
async def handle_message(update, context): 
    if update.message and update.message.text: await process_reply(update, context, update.message.text)
async def handle_voice(update, context):
    if update.message.voice:
        file = await update.message.voice.get_file()
        text = await transcribe_audio(await file.download_as_bytearray())
        await process_reply(update, context, text, is_voice_input=True)
async def handle_photo(update, context):
    if update.message.photo:
        photo = await update.message.photo[-1].get_file()
        base64_img = base64.b64encode(await photo.download_as_bytearray()).decode('utf-8')
        await process_reply(update, context, user_text=None, is_photo_input=True, photo_base64=base64_img)

async def error_handler(update, context):
    print(f"🚨 Error: {context.error}")
    if ADMIN_ID: 
        try: await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 錯誤：\n{context.error}")
        except: pass

async def send_active_message(context):
    for chat_id, state in user_states.items():
        if state.get("is_sleeping") or state.get("active_count", 0) >= 2: continue
        state["time_val"] += 1
        if random.random() > 0.3: continue
        state["active_count"] += 1
        
        # 30% 機率分享新聞
        rand_val = random.random()
        trigger = ""
        if rand_val < 0.3:
            news = get_latest_news()
            trigger = f"【指令：分享情報】妳剛剛在網路上看到了這個：\n{news}\n請跟落卿分享，並說說妳的看法。雙語格式。"
        elif rand_val < 0.5: trigger = "【指令：依賴】覺得寂寞，問落卿在幹嘛。"
        else: trigger = "【指令：撒嬌】想聽落卿的聲音。"

        prompt = get_base_persona() + "\n" + get_current_prompt()
        messages = [{"role": "system", "content": prompt}] + chat_history.get(chat_id, []) + [{"role": "system", "content": trigger}]
        
        if state.get("voice_mode"): await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
        else: await context.bot.send_chat_action(chat_id=chat_id, action='typing')

        full_res = get_dual_core_response(messages, "日常") # 新聞分享也用 DeepSeek 處理即可，省錢
        if "|||" in full_res: cn, jp = full_res.split("|||", 1)
        else: cn, jp = full_res, full_res
        
        await context.bot.send_message(chat_id=chat_id, text=cn.strip())
        if state.get("voice_mode"):
            voice = await generate_elevenlabs_audio(jp.strip())
            if voice: await context.bot.send_voice(chat_id=chat_id, voice=voice)
        
        if chat_id in chat_history: chat_history[chat_id].append({"role": "assistant", "content": full_res})
        save_memory()

# 定時任務
async def daily_morning(context):
    get_latest_news() # 早上刷新一次新聞
    for cid in user_states: user_states[cid].update({"is_sleeping": False, "active_count": 0})
async def daily_night(context):
    for cid in user_states: user_states[cid]["is_sleeping"] = True

if __name__ == '__main__':
    load_memory()
    get_latest_news() # 啟動時先抓一次
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(error_handler)
    
    jq = app.job_queue
    tz = pytz.timezone('Asia/Taipei')
    jq.run_repeating(send_active_message, interval=300, first=10)
    jq.run_daily(daily_morning, time=time(7, 30, tzinfo=tz))
    jq.run_daily(daily_night, time=time(0, 0, tzinfo=tz))
    
    print("✅ 佐奈聰音 V19.2 (聯網實時話題版) 已上線！")
    app.run_polling()
