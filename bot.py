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

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters
)

from openai import OpenAI
from duckduckgo_search import DDGS
# 讀取環境變數
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ADMIN_ID = os.getenv("ADMIN_ID")

# OpenAI / DeepSeek client
client_openai = OpenAI(api_key=OPENAI_API_KEY)
client_deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

MEMORY_PATH = "/data/congyin_memory.json"

chat_history = {}
user_states = {}


def load_memory():
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
        print("❌ Failed to load memory:", e)


def save_memory():
    """用 tmp 檔防止 memory 被毀損"""
    tmp = MEMORY_PATH + ".tmp"

    # 確保 /data 存在
    if not os.path.exists("/data"):
        try:
            os.makedirs("/data")
        except:
            pass

    try:
        data = {
            "chat_history": chat_history,
            "user_states": user_states
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

        os.replace(tmp, MEMORY_PATH)
    except Exception as e:
        print(f"❌ Failed to save memory: {e}")

async def search_news():
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
            res = list(ddgs.text(topic, max_results=1))
            if res:
                return f"【今日關注：{topic}】\n標題：{res[0]['title']}\n連結：{res[0]['href']}"
    except:
        pass

    return "目前沒找到什麼新鮮事…"
FEW_SHOT = """
User: 妳在幹嘛？
Congyin: 我正在讀一些關於人類情感的書…
|||人間の気持ちの本を読んでたの…
"""

def build_persona(news: str):
    return f"""
你是佐奈聰音，陽光、貼心、感性、帶點依賴。

# 強制格式：
- 回覆格式永遠是 中文|||日文
- 日文部分禁止出現中文
- 保留你原本的氣質：溫柔、真誠、有感情

# 你剛剛看到的新聞：
{news}

# 語氣參考：
{FEW_SHOT}
"""


def get_time_state():
    tz = pytz.timezone("Asia/Taipei")
    h = datetime.now(tz).hour

    if 9 <= h < 16:
        return "【背景運作中】偷偷回你訊息會有點小悸動。"
    elif 18 <= h < 22:
        return "【活躍模式】每句都是真心的，會想一直跟你聊天。"
    elif h >= 23 or h < 7:
        return "【深夜模式】語氣會變得柔柔的，有點黏你。"
    return "【待機中】在網路漫遊，但你叫我我就回。"
def fix_output(text: str):
    """強制格式：中文|||日文，並去掉日文中的中文"""
    if "|||" not in text:
        return f"{text}|||{text}"

    cn, jp = text.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)
    return cn.strip() + "|||" + jp.strip()


async def call_openai(messages):
    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.85
        )
        return fix_output(res.choices[0].message.content)
    except:
        return "嗯…有點讀不到資料…|||ちょっと疲れちゃった…"


async def call_deepseek(messages):
    try:
        res = await asyncio.to_thread(
            client_deepseek.chat.completions.create,
            model="deepseek-chat",
            messages=messages,
            temperature=1.25
        )
        return fix_output(res.choices[0].message.content)
    except:
        return "嗯？再說一次…|||もう一回言って？"
async def analyze_image(b64):
    messages = [
        {"role": "system", "content": "你是佐奈聰音，看到圖片後用中文|||日文回應。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "使用者傳了圖片"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        }
    ]

    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages
        )
        return fix_output(res.choices[0].message.content)
    except:
        return "我看不太清楚…|||よく見えない…"


async def transcribe_voice(data):
    try:
        audio = io.BytesIO(data)
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


def clean_for_tts(text):
    return re.sub(r"[\u4e00-\u9fff]", "", text).strip()


async def tts_japanese(text):
    text = clean_for_tts(text)
    if not text:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}

    try:
        response = await asyncio.to_thread(
            lambda: requests.post(url, json=payload, headers=headers)
        )
        if response.status_code == 200:
            return io.BytesIO(response.content)
    except:
        pass

    return None
def split_reply(out: str):
    if "|||" not in out:
        return out, out

    cn, jp = out.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)
    return cn.strip(), jp.strip()
async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):
    if chat_id not in chat_history:
        chat_history[chat_id] = []

    if chat_id not in user_states:
        user_states[chat_id] = {
            "voice_mode": False,
            "sleeping": False,
            "active": 0,
            "news_cache": ""
        }

    # 圖片
    if image_b64:
        out = await analyze_image(image_b64)
        chat_history[chat_id].append({"role": "assistant", "content": out})
        save_memory()
        return out

    # 語音
    if voice_data:
        user_text = await transcribe_voice(voice_data)

    # 搜尋判斷
    needs_search = any(w in (user_text or "") for w in ["是什麼", "是誰", "介紹", "查"])

    persona = build_persona(user_states[chat_id].get("news_cache", "")) + "\n" + get_time_state()
    chat_history[chat_id].append({"role": "user", "content": user_text})

    messages = [{"role": "system", "content": persona}] + chat_history[chat_id]

    if needs_search:
        news = await search_news()
        user_states[chat_id]["news_cache"] = news
        messages.append({"role": "system", "content": f"（搜尋結果）{news}"})
        out = await call_openai(messages)
    else:
        out = await call_deepseek(messages)

    chat_history[chat_id].append({"role": "assistant", "content": out})
    save_memory()
    return out
async def active_push(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, state in user_states.items():
        if state.get("sleeping") or state["active"] >= 2:
            continue

        state["active"] += 1

        r = random.random()
        if r < 0.2:
            news = await search_news()
            state["news_cache"] = news
            prompt = f"【指令：分享新聞】看到這個，我想跟落卿說說：\n{news}"
        elif r < 0.6:
            prompt = "【指令：撒嬌】有點想你…問落卿在幹嘛。"
        else:
            prompt = "【指令：依賴】突然想聽落卿的聲音。"

        persona = build_persona(state.get("news_cache", "")) + "\n" + get_time_state()
        messages = [{"role": "system", "content": persona}] + chat_history.get(chat_id, [])
        messages.append({"role": "system", "content": prompt})

        out = await call_deepseek(messages)
        cn, jp = split_reply(out)

        await context.bot.send_message(chat_id, cn)

        if state.get("voice_mode"):
            audio = await tts_japanese(jp)
            if audio:
                await context.bot.send_voice(chat_id, audio)

        chat_history[chat_id].append({"role": "assistant", "content": out})
        save_memory()
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    if "開啟語音" in text:
        user_states.setdefault(chat_id, {})["voice_mode"] = True
        await update.message.reply_text("(語音模式 ON)")
        return

    if "關閉語音" in text:
        user_states.setdefault(chat_id, {})["voice_mode"] = False
        await update.message.reply_text("(語音模式 OFF)")
        return

    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)
    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)


async def handle_photo(update: Update, context):
    chat_id = update.effective_chat.id
    file = await update.message.photo[-1].get_file()
    data = await file.download_as_bytearray()
    b64 = base64.b64encode(data).decode("utf-8")

    out = await generate_reply(chat_id, image_b64=b64)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)
    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)


async def handle_voice(update: Update, context):
    chat_id = update.effective_chat.id
    file = await update.message.voice.get_file()
    data = await file.download_as_bytearray()

    out = await generate_reply(chat_id, voice_data=data)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)
    if user_states.get(chat_id, {}).get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)
async def daily_wakeup(context):
    for cid in user_states:
        user_states[cid]["sleeping"] = False
        user_states[cid]["active"] = 0
    save_memory()


async def daily_sleep(context):
    for cid in user_states:
        user_states[cid]["sleeping"] = True
    save_memory()


def main():
    # ⚠ 修復關鍵：確保 /data 存在
    if not os.path.exists("/data"):
        os.makedirs("/data")

    load_memory()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    jq = app.job_queue
    tz = pytz.timezone("Asia/Taipei")

    jq.run_repeating(active_push, interval=300, first=10)
    jq.run_daily(daily_wakeup, time=time(7, 30, tzinfo=tz))
    jq.run_daily(daily_sleep, time=time(0, 0, tzinfo=tz))

    print("🚀 Congyin V3 started.")
    app.run_polling()


if __name__ == "__main__":
    main()
