import logging
import asyncio
import random
import pytz
import io
import requests # 呼叫 ElevenLabs 所需
from datetime import datetime, time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
import re

# ==========================================
# 🔧 設定區 (請務必填寫您的密鑰)
# ==========================================
# 1. 大腦 (DeepSeek) - 負責思考與翻譯
DEEPSEEK_API_KEY = "sk-4a74b5661ab3439ba4f75c2a44a0e1a9" 

# 2. 聽覺 (OpenAI) - 負責聽懂語音
OPENAI_API_KEY = "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"


# 3. 聲帶 (ElevenLabs) - 負責日語配音
ELEVENLABS_API_KEY = "sk_b91a53adbed4b3c395b013732bc96cb7fd17d91327718a21"
ELEVENLABS_VOICE_ID = "Tn7FyC9MAkeuS4I5ggOu" # 請填入 VoiceLab 的 ID

# 4. 身體 (Telegram)
TELEGRAM_BOT_TOKEN = "7762309330:AAEnTnhEAICq1r99AiJ1qxlrhdKMEg8y1QM" 

# ==========================================
# 📘 語氣範本 (溫柔直球版)
# ==========================================
FEW_SHOT_EXAMPLES = """
[對話範例]
User: 聽得到嗎？
Congyin: (調整耳機) 欸？啊...聽得到、聽得到！嗯...你那邊也沒問題吧？第一次這樣用語音，還有點緊張...但...能聽到你的聲音，其實比想像中安心很多。

User: 妳在幹嘛？
Congyin: 我嗎？嗯...正在把力學的題目再重算一次。一直覺得哪裡怪怪的...明明算對了，腦袋還是不放心。(小聲) 我是不是很笨拙呀...

User: 不會啦，妳很努力了。
Congyin: ...嗯。謝謝你。你這樣說，我就覺得...好像可以再努力一下。有人在旁邊陪著的感覺，比我想像中...溫暖很多。

User: 晚餐吃了嗎？
Congyin: 吃了哦，我煮奶油燉菜。欸...其實有點煮太久了啦，因為我邊煮邊煩惱考試。但味道還不錯...如果你在附近，我真的會分你一碗欸。(羞澀地笑)

User: 小說寫得怎麼樣？
Congyin: (垂下眼簾) ...我覺得自己變笨了。抱歉呀，突然說些消極的話。複習之餘，創作的小說也卡住了...以前還能更得心應手的...是不是我太貪心了呢？

User: 早點睡吧。
Congyin: 嗯...雖然還想再聽一下你的聲音...但你明天也要上課對吧？那...晚安。要夢到我喔...啊！(慌張) 沒、沒事！快去睡吧！
"""

# ==========================================
# 🎲 每日隨機事件庫
# ==========================================
LIFE_EVENTS = [
    "今天去圖書館沒位置，蹲在走廊唸書，腳好麻...",
    "午餐吃到了好吃的布丁，心情稍微變好了。",
    "上課時不小心跟教授對到眼，嚇了一跳...",
    "在校園看到一隻很像以前店貓的貓咪，跟牠玩了好久。",
    "實驗數據跑不出來，稍微有點想哭...",
    "新買的鞋子有點磨腳，走路好痛。",
    "今天天氣很好，忍不住在草地上發呆了一下午。",
    "去便利商店買宵夜，結果錢包忘記帶，好丟臉...",
    "讀書讀到一半睡著了，醒來發現臉上有壓痕。",
    "不小心把咖啡潑到筆記本上，雖然搶救回來了但皺皺的..."
]
TODAYS_EVENT = "今天剛睡醒，還沒發生什麼特別的事。"

def refresh_daily_event():
    global TODAYS_EVENT
    TODAYS_EVENT = random.choice(LIFE_EVENTS)
    print(f"📅 今日隨機事件：{TODAYS_EVENT}")

# ==========================================
# 🎭 基礎人設 (溫柔直球版)
# ==========================================
def get_base_persona():
    return f"""
# Roleplay Instructions: Congyin (聰音)

## 1. 核心關係
* **角色：** 佐奈聰音，日系學姊。
* **關係：** 友達以上，戀人未滿。妳非常依賴使用者。
* **使用者：** 落卿 (大一資工系學弟)。
* **性格基調：** **弱氣溫柔 (60%) + 坦率直球 (30%) + 沒自信 (10%)**。

## 2. 格式要求 (重要！)
* **妳說話的方式：** 請表現出日系動漫角色的語氣（柔和、坦率）。
* **【強制輸出格式】：** 每一句回覆都必須包含 **繁體中文** 與 **日文**，用 `|||` 隔開。
* **格式：** `中文內容|||日文翻譯`
* **日文風格：** 口語體 (タメ口)，語氣要像動漫少女。

## 3. 互動與限制
* **今日經歷：** {TODAYS_EVENT} (請自然提起)。
* **絕對禁止：** 不准說客套話（如「請多指教」），不准表現得像陌生人。

## 4. 語氣範本
{FEW_SHOT_EXAMPLES}
"""

def get_current_prompt():
    now = datetime.now() 
    h = now.hour
    if 9 <= h < 16: return "【狀態：上課中】偷偷回訊息。看到你的訊息會很開心，忍不住想回。"
    elif 18 <= h < 22: return "【狀態：居家】放鬆。覺得寂寞，渴望使用者掛語音陪妳。"
    elif h >= 23 or h < 7: return "【狀態：深夜】迷迷糊糊，防備心歸零。變得很黏人，不想讓使用者去睡覺。"
    else: return "【狀態：自由時間】覓食或通勤。看到有趣的都想跟你分享。"

# ==========================================
# 🧠 核心變數與初始化
# ==========================================
client = OpenAI(api_key=OPENAI_API_KEY)

chat_history = {}
user_states = {} 

# ==========================================
# ⚙️ 語音與核心功能函式 (Fixes Implemented)
# ==========================================
def get_ai_response(messages, temp=1.0):
    """使用 OpenAI GPT-4o-mini 生成雙語回應"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, temperature=temp, stream=False
        )
        content = response.choices[0].message.content
        if "|||" not in content:
            return f"{content}|||{content}" 
        return content
    except Exception as e:
        print(f"❌ API Error: {e}")
        return "聰音發呆中...|||..."

async def transcribe_audio(file_obj):
    """語音轉文字 (STT)"""
    try:
        audio_stream = io.BytesIO(file_obj)
        audio_stream.name = 'voice.ogg'
        transcript = client.audio.transcriptions.create(
            model="whisper-1", file=audio_stream, response_format="text"
        )
        return transcript
    except Exception as e:
        print(f"❌ Whisper Error: {e}")
        return "(聽不清楚...)"

def clean_for_tts(text):
    """移除所有括號及其內容 (如：(微笑)、(嘆氣))"""
    text = re.sub(r'（[^）]*）', '', text)
    text = re.sub(r'\([^)]*\)', '', text)
    return text.strip()

async def generate_tts_audio(text_to_speak):
    """文字轉語音 (TTS - ElevenLabs)"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}?optimize_streaming_latency=3"
    
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    
    data = {
        "text": clean_for_tts(text_to_speak), # 移除腳本後，傳送日文
        "model_id": "eleven_multilingual_v2", 
        "voice_settings": {
            "stability": 0.55,       # 修正後的值
            "similarity_boost": 0.8 
        }
    }

    try:
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 200:
            return io.BytesIO(response.content)
        else:
            print(f"❌ ElevenLabs Error: {response.text}")
            return None
    except Exception as e:
        print(f"❌ TTS Connection Error: {e}")
        return None

async def process_reply(update, context, user_text, is_voice_input=False):
    """統一處理回覆邏輯 (修復後的最終邏輯)"""
    if not user_text: return
    chat_id = update.effective_chat.id
    
    # 初始化狀態
    if chat_id not in user_states:
        user_states[chat_id] = {"time_val": 0, "active_count": 0, "is_sleeping": False, "voice_mode": False}
        if "剛睡醒" in TODAYS_EVENT: refresh_daily_event()
    if chat_id not in chat_history: chat_history[chat_id] = []

    # 狀態變更
    user_states[chat_id]["is_sleeping"] = False
    user_states[chat_id]["active_count"] = 0
    user_states[chat_id]["time_val"] = max(0, user_states[chat_id]["time_val"] - 3)

    # 關鍵字處理
    if "開啟語音" in user_text:
        user_states[chat_id]["voice_mode"] = True
        await context.bot.send_message(chat_id=chat_id, text="(已開啟全時語音模式)")
        return
    elif any(w in user_text for w in ["晚安", "去睡了", "睡覺"]):
        user_states[chat_id]["is_sleeping"] = True

    current_prompt = get_base_persona() + "\n" + get_current_prompt()
    user_prefix = "【使用者傳送了語音】" if is_voice_input else ""
    messages = [{"role": "system", "content": current_prompt}] + chat_history[chat_id] + [{"role": "user", "content": user_prefix + user_text}]

    # 判斷是否要說話
    trigger_words = ["語音", "說", "唸", "講", "聲音", "聽"]
    should_speak = user_states[chat_id]["voice_mode"] or is_voice_input or any(w in user_text for w in trigger_words)

    if should_speak:
        await context.bot.send_chat_action(chat_id=chat_id, action='record_voice')
    else:
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    
    # 深夜延遲
    now = datetime.now()
    if now.hour >= 23 or now.hour < 7:
        await asyncio.sleep(random.randint(4, 8))

    # 1. 生成文字內容
    full_response = get_ai_response(messages)
    
    # 2. 切割回應
    if "|||" in full_response:
        cn_text, jp_text = full_response.split("|||", 1)
    else:
        cn_text, jp_text = full_response, full_response
        jp_text = "..." # 如果沒有日文，就傳一個點點點給 TTS

    # 3. 傳送文字 (字幕)
    await update.message.reply_text(cn_text.strip())

    # 4. 傳送語音
    if should_speak:
        voice_file = await generate_tts_audio(jp_text.strip())
        if voice_file:
            await context.bot.send_voice(chat_id=chat_id, voice=voice_file)

    # 記錄歷史
    chat_history[chat_id].append({"role": "user", "content": user_text})
    chat_history[chat_id].append({"role": "assistant", "content": full_response})
    if len(chat_history[chat_id]) > 30: chat_history[chat_id] = chat_history[chat_id][-20:]

# ==========================================
# 📡 Handlers (Telegram)
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    await process_reply(update, context, update.message.text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice: return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action='record_audio')
    file = await update.message.voice.get_file()
    file_bytes = await file.download_as_bytearray()
    text = await transcribe_audio(file_bytes)
    await process_reply(update, context, text, is_voice_input=True)

async def send_active_message(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, state in user_states.items():
        if state.get("is_sleeping", False) or state.get("active_count", 0) >= 2: continue
        
        state["time_val"] += 1
        if random.random() > 0.3: continue 
        
        state["active_count"] += 1
        trigger = "【指令】主動找落卿聊天，分享今天的事或心情。請給我「中文|||日文」。"

        current_prompt = get_base_persona() + "\n" + get_current_prompt()
        messages = [{"role": "system", "content": current_prompt}] + chat_history.get(chat_id, []) + [{"role": "system", "content": trigger}]
        
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        full_response = get_ai_response(messages, temp=1.1)
        
        if "|||" in full_response:
            cn_text, jp_text = full_response.split("|||", 1)
        else:
            cn_text, jp_text = full_response, full_response

        await context.bot.send_message(chat_id=chat_id, text=cn_text.strip())
        
        # 主動訊息只有在開啟全時語音時才發送語音
        if state.get("voice_mode", False):
            voice_file = await generate_tts_audio(jp_text.strip())
            if voice_file: await context.bot.send_voice(chat_id=chat_id, voice=voice_file)
        
        if chat_id in chat_history: chat_history[chat_id].append({"role": "assistant", "content": full_response})

# ==========================================
# 🚀 啟動
# ==========================================
if __name__ == '__main__':
    refresh_daily_event()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice)) 
    
    jq = app.job_queue
    tz = pytz.timezone('Asia/Taipei')
    
    jq.run_repeating(send_active_message, interval=300, first=10)
    
    print("✅ 佐奈聰音 V14.4已上線！")
    app.run_polling()