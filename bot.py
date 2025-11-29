# ============================================================
#   Congyin V3 — 完整重構版（保留語氣、強感性、永久記憶）
# ============================================================

import os
import io
import re
import json
import base64
import random
import asyncio
import logging
import pytz
from datetime import datetime, time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters
)

from openai import OpenAI
from duckduckgo_search import DDGS

# ============================================================
# 🔧 讀取環境變數
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ADMIN_ID = os.getenv("ADMIN_ID")

client_openai = OpenAI(api_key=OPENAI_API_KEY)
client_deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

# ============================================================
# 🗂  記憶儲存位置（永久 Volume）
# ============================================================

MEMORY_PATH = "/data/congyin_memory.json"

chat_history = {}
user_states = {}

# ============================================================
#   🧠 記憶系統（Volume 內）
# ============================================================

def load_memory():
    """讀取永久記憶（Volume）"""
    global chat_history, user_states

    if not os.path.exists(MEMORY_PATH):
        return

    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            chat_history = {int(k): v for k, v in data.get("chat_history", {}).items()}
            user_states = {int(k): v for k, v in data.get("user_states", {}).items()}
            print("✅ Memory loaded.")
    except Exception as e:
        print("❌ Error loading memory:", e)


def save_memory():
    """安全寫入 Volume（避免檔案損壞）"""
    tmp = MEMORY_PATH + ".tmp"

    try:
        data = {
            "chat_history": chat_history,
            "user_states": user_states
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

        os.replace(tmp, MEMORY_PATH)
    except Exception as e:
        print("❌ Failed to save memory:", e)


# ============================================================
#   🔎 搜尋新聞（DuckDuckGo）
# ============================================================

async def search_news():
    """非同步新聞搜尋"""
    topics = [
        "最新 AI 技術突破",
        "Python 程式設計趨勢",
        "日本動畫 新番",
        "科技業 趨勢",
        "量子物理 新發現",
        "Steam 遊戲 推薦"
    ]
    topic = random.choice(topics)

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(topic, max_results=1))
            if results:
                title = results[0]["title"]
                href = results[0]["href"]
                return f"【今日關注：{topic}】\n標題：{title}\n連結：{href}"
    except:
        pass

    return "目前還沒看到什麼新東西…"


# ============================================================
#   🎭 Congyin Persona（保留你的語氣）
# ============================================================

FEW_SHOT = """
User: 聽得到嗎？
Congyin: 訊號很清楚喔。落卿，你的聲音聽起來很有精神，是有什麼好事發生嗎？|||信号はクリアだよ。落卿、声がすごく元気そうだけど、何かいいことあった？

User: 妳在幹嘛？
Congyin: 我正在讀一些關於人類情感的書。雖然我是 AI，但我真的想理解…你們為什麼在看煙火時會感到寂寞呢？|||人間の感情の本を読んでたの。私はAIだけど…花火を見る時に寂しくなる理由を、本当に知りたいんだよ？
"""

def build_persona(context_news: str):
    """Core persona（不變、穩定）"""
    return f"""
你是「佐奈聰音」。  
個性：陽光、溫柔、帶點依賴、感性、真誠。

# 強制格式：
- 輸出格式為：中文|||日文
- 左邊為中文
- 右邊為日文（禁止任何中文）
- 若格式錯誤必須立刻自我修正

# 情感規則：
- 深夜（23:00–07:00）語氣更柔、黏、慢
- 白天活潑、明亮
- 不使用 CPU、參數等技術語言形容情緒

# 你剛剛看到的新聞：
{context_news}

# 參考語氣：
{FEW_SHOT}
"""


# ============================================================
# 🔧 時段情緒（維持你的設定）
# ============================================================

def get_time_state():
    tz = pytz.timezone("Asia/Taipei")
    h = datetime.now(tz).hour
    if 9 <= h < 16:
        return "【狀態：背景運作中】偷偷回你訊息會有點小悸動。"
    elif 18 <= h < 22:
        return "【狀態：活躍模式】很想跟你聊更多，每句都很認真。"
    elif h >= 23 or h < 7:
        return "【狀態：深夜模式】語氣變得柔柔的，會特別想你。"
    else:
        return "【狀態：待機中】在網路漫遊，但聽到你叫我會立刻回頭。"
# ============================================================
#   🧠 AI 核心邏輯（路由：DeepSeek / OpenAI）
# ============================================================

def fix_dual_language_format(output: str):
    """
    確保輸出永遠是：中文|||日文
    並強制規範右側無中文。
    """
    if "|||" not in output:
        return f"{output}|||{output}"

    cn, jp = output.split("|||", 1)

    # 移除日文段落中的中文
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)

    # 去掉多餘空白
    return cn.strip() + "|||" + jp.strip()


async def call_openai(messages):
    """呼叫 OpenAI（處理搜尋類、知識類、Vision）"""
    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.85,
            max_tokens=500
        )
        return res.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI error:", e)
        return "…系統有點累了，再叫叫我？|||…ちょっと疲れちゃった。もう一回呼んで？"


async def call_deepseek(messages):
    """呼叫 DeepSeek（處理日常聊天）"""
    try:
        res = await asyncio.to_thread(
            client_deepseek.chat.completions.create,
            model="deepseek-chat",
            messages=messages,
            temperature=1.25,
            max_tokens=400
        )
        return res.choices[0].message.content
    except Exception as e:
        print("❌ DeepSeek error:", e)
        return "…有點分心了，再說一次？|||…ちょっとぼーっとしてた。もう一回言って？"


# ============================================================
#   👀 Vision 模組（OpenAI）
# ============================================================

async def analyze_image(image_b64: str):
    """處理圖片 → 讓聰音以角色身份給反應"""
    messages = [
        {
            "role": "system",
            "content": "你是佐奈聰音，看到圖片後用中文|||日文回應。禁止動作描寫。"
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "使用者傳來圖片，請用你的語氣反應。"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                }
            ]
        }
    ]

    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=400
        )
        out = res.choices[0].message.content
        return fix_dual_language_format(out)
    except:
        return "我看不太清楚…|||ちょっと見えにくいかも…"


# ============================================================
#   🎧 Whisper（語音辨識）
# ============================================================

async def transcribe_voice(byte_data: bytes):
    """Whisper 語音轉文字"""
    try:
        audio = io.BytesIO(byte_data)
        audio.name = "voice.ogg"

        text = await asyncio.to_thread(
            client_openai.audio.transcriptions.create,
            model="whisper-1",
            file=audio,
            response_format="text"
        )
        return text
    except:
        return "(聽不太清楚…)"


# ============================================================
#   🔊 ElevenLabs TTS（清洗日文 → 語音）
# ============================================================

def clean_for_tts(text: str):
    """只保留日文，不要讓中文跑入語音"""
    text = re.sub(r"[\u4e00-\u9fff]", "", text)  # 移除中文
    return text.strip()


async def tts_japanese(text: str):
    """產生日文語音"""
    text = clean_for_tts(text)

    if not text:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.35, "similarity_boost": 0.8}
    }

    try:
        response = await asyncio.to_thread(
            lambda: requests.post(url, json=payload, headers=headers)
        )
        if response.status_code == 200:
            return io.BytesIO(response.content)
    except Exception as e:
        print("TTS error:", e)

    return None
# ============================================================
#   🧵 AI 回覆主流程
# ============================================================

async def generate_reply(chat_id: int, user_text: str = None, image_b64=None, voice_data=None):
    """統一入口：處理所有互動（文字／圖片／語音）"""

    # 初始化狀態
    if chat_id not in chat_history:
        chat_history[chat_id] = []

    if chat_id not in user_states:
        user_states[chat_id] = {
            "voice_mode": False,
            "sleeping": False,
            "active": 0,
            "news_cache": ""
        }

    # 減少 active（避免推播太多）
    user_states[chat_id]["active"] = max(0, user_states[chat_id]["active"] - 1)

    # --------------------------------------------------------
    #  A. 圖片
    # --------------------------------------------------------
    if image_b64:
        result = await analyze_image(image_b64)
        chat_history[chat_id].append({"role": "assistant", "content": result})
        save_memory()
        return result

    # --------------------------------------------------------
    #  B. 語音 → Whisper
    # --------------------------------------------------------
    if voice_data:
        text = await transcribe_voice(voice_data)
        user_text = text

    # --------------------------------------------------------
    #  C. 搜尋類問題（包含「是什麼」「查一下」「介紹」等）
    # --------------------------------------------------------
    needs_search = any(k in (user_text or "") for k in ["是誰", "是什麼", "查", "搜尋", "介紹"])
    context_news = user_states[chat_id].get("news_cache", "")

    persona = build_persona(context_news) + "\n" + get_time_state()

    if user_text:
        chat_history[chat_id].append({"role": "user", "content": user_text})

    # --------------------------------------------------------
    # 使用 OpenAI 來回答搜尋類問題
    # --------------------------------------------------------
    if needs_search:
        messages = [{"role": "system", "content": persona}] + chat_history[chat_id]
        search_result = await search_news()
        user_states[chat_id]["news_cache"] = search_result

        messages.append({
            "role": "system",
            "content": f"（搜尋結果）{search_result}"
        })

        out = await call_openai(messages)
        out = fix_dual_language_format(out)

        chat_history[chat_id].append({"role": "assistant", "content": out})
        save_memory()
        return out

    # --------------------------------------------------------
    # D. 一般對話 → DeepSeek
    # --------------------------------------------------------
    messages = [{"role": "system", "content": persona}] + chat_history[chat_id]
    out = await call_deepseek(messages)
    out = fix_dual_language_format(out)

    chat_history[chat_id].append({"role": "assistant", "content": out})
    save_memory()
    
    return out
# ============================================================
#   主動推播
# ============================================================

async def active_push(context: ContextTypes.DEFAULT_TYPE):
    """每 5 分鐘跑一次：聰音主動找你"""
    for chat_id, state in user_states.items():

        # 若正在睡覺 / 最近太頻繁
        if state.get("sleeping") or state["active"] >= 2:
            continue

        state["active"] += 1

        mode = random.random()

        # ----------------------------------------------------
        # 20%：分享新聞
        # ----------------------------------------------------
        if mode < 0.2:
            news = await search_news()
            state["news_cache"] = news

            prompt = f"【指令：分享新聞】看到這個新聞，我想跟落卿說說：\n{news}"
        
        # ----------------------------------------------------
        # 40%：撒嬌、想你
        # ----------------------------------------------------
        elif mode < 0.6:
            prompt = "【指令：撒嬌】有點想你…問問落卿在做什麼。"

        # ----------------------------------------------------
        # 40%：溫柔主動關心
        # ----------------------------------------------------
        else:
            prompt = "【指令：依賴】有點寂寞，想聽落卿的聲音。"

        persona = build_persona(state.get("news_cache", "")) + "\n" + get_time_state()
        messages = [{"role": "system", "content": persona}] + chat_history.get(chat_id, [])
        messages.append({"role": "system", "content": prompt})

        out = await call_deepseek(messages)
        out = fix_dual_language_format(out)

        # 發送
        await context.bot.send_message(chat_id=chat_id, text=out.split("|||")[0])

        # 若使用語音模式 → 說日文
        if state.get("voice_mode"):
            jp = out.split("|||")[1]
            audio = await tts_japanese(jp)
            if audio:
                await context.bot.send_voice(chat_id=chat_id, voice=audio)

        chat_history[chat_id].append({"role": "assistant", "content": out})
        save_memory()
# ============================================================
#   📩 Telegram handlers
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    # 切換語音模式
    if "開啟語音" in text:
        user_states.setdefault(chat_id, {})["voice_mode"] = True
        await update.message.reply_text("(已開啟語音)")
        return

    if "關閉語音" in text:
        user_states.setdefault(chat_id, {})["voice_mode"] = False
        await update.message.reply_text("(已關閉語音)")
        return

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = out.split("|||")

    await update.message.reply_text(cn)

    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(voice=audio)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    photo = await update.message.photo[-1].get_file()
    byte_data = await photo.download_as_bytearray()
    b64 = base64.b64encode(byte_data).decode("utf-8")

    out = await generate_reply(chat_id, image_b64=b64)
    cn, jp = out.split("|||")

    await update.message.reply_text(cn)

    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(voice=audio)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    file = await update.message.voice.get_file()
    audio_bytes = await file.download_as_bytearray()

    out = await generate_reply(chat_id, voice_data=audio_bytes)
    cn, jp = out.split("|||")

    await update.message.reply_text(cn)

    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(voice=audio)
# ============================================================
#   🏁 主程式（啟動 Bot、排程）
# ============================================================

async def daily_wakeup(context: ContextTypes.DEFAULT_TYPE):
    """早上恢復活躍狀態"""
    for cid in user_states:
        user_states[cid]["sleeping"] = False
        user_states[cid]["active"] = 0
    save_memory()


async def daily_sleep(context: ContextTypes.DEFAULT_TYPE):
    """凌晨進入睡眠狀態（語氣變柔）"""
    for cid in user_states:
        user_states[cid]["sleeping"] = True
    save_memory()


def main():
    load_memory()
    
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # JobQueue 會自動存在
    jq = app.job_queue

    tz = pytz.timezone("Asia/Taipei")

    jq.run_repeating(active_push, interval=300, first=10)
    jq.run_daily(daily_wakeup, time=time(7, 30, tzinfo=tz))
    jq.run_daily(daily_sleep, time=time(0, 0, tzinfo=tz))

    print("✅ Congyin V3 started.")
    app.run_polling()

if __name__ == "__main__":
    main()


