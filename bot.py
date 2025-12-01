import os
import io
import re
import json
import base64
import asyncio
import random
import logging
import pytz
import redis
from datetime import datetime, time

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters, ContextTypes
)

from openai import OpenAI
from duckduckgo_search import DDGS

# ----------------------------------------------------------
# 環境變數
# ----------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ADMIN_ID = os.getenv("ADMIN_ID")

REDIS_HOST = os.getenv("REDISHOST")
REDIS_PORT = int(os.getenv("REDISPORT"))
REDIS_PASSWORD = os.getenv("REDISPASSWORD")

# ----------------------------------------------------------
# 客戶端初始化
# ----------------------------------------------------------

client_openai = OpenAI(api_key=OPENAI_API_KEY)
client_deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

# ----------------------------------------------------------
# Redis 初始化（具備自動重試 + fallback）
# ----------------------------------------------------------

def init_redis():
    try:
        r = redis.Redis(
            host=REDIS_HOST,
            port=int(REDIS_PORT),
            password=REDIS_PASSWORD if REDIS_PASSWORD else None,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        r.ping()
        print("✅ Redis 連線成功")
        return r
    except Exception as e:
        print("❌ Redis 連線失敗，啟動 fallback RAM:", e)
        return None

redis_client = init_redis()

# fallback RAM（當 Redis 掛掉使用）
memory_fallback = {
    "history": {},
    "state": {},
}

# ----------------------------------------------------------
# Redis 儲存系統 — 安全版（pipeline + fallback）
# ----------------------------------------------------------

def save_history(chat_id, history):
    """ 儲存聊天歷史（限 40 則） """

    history = history[-40:]  # 避免 Redis 爆掉

    if redis_client:
        try:
            pipe = redis_client.pipeline()
            pipe.set(f"history:{chat_id}", json.dumps(history))
            pipe.execute()
            return
        except:
            pass  # fallback

    memory_fallback["history"][chat_id] = history

def load_history(chat_id):
    if redis_client:
        try:
            raw = redis_client.get(f"history:{chat_id}")
            if raw:
                return json.loads(raw)
        except:
            pass
    return memory_fallback["history"].get(chat_id, [])

def save_state(chat_id, state):
    if redis_client:
        try:
            pipe = redis_client.pipeline()
            pipe.set(f"state:{chat_id}", json.dumps(state))
            pipe.execute()
            return
        except:
            pass
    memory_fallback["state"][chat_id] = state

def load_state(chat_id):
    if redis_client:
        try:
            raw = redis_client.get(f"state:{chat_id}")
            if raw:
                return json.loads(raw)
        except:
            pass
    # 預設多一個 care_mode
    return memory_fallback["state"].get(chat_id, {
        "voice_mode": False,
        "sleeping": False,
        "active": 0,
        "news_cache": "",
        "care_mode": False,   # 是否啟動承接層
    })

# ----------------------------------------------------------
# 工具：取得現在時間（台北時區）
# ----------------------------------------------------------

def now_taipei():
    tz = pytz.timezone("Asia/Taipei")
    return datetime.now(tz)

# ----------------------------------------------------------
# 時間人格（C 階段：完整 AI 時間自覺）
# ----------------------------------------------------------

def time_personality():
    t = now_taipei()
    h = t.hour

    if 5 <= h < 9:
        return "【早晨人格】語氣清淡、透明、像剛醒來但精神清楚。"
    elif 9 <= h < 16:
        return "【白天人格】語氣明亮、反應快、帶有活力。"
    elif 16 <= h < 20:
        return "【傍晩人格】語氣柔軟、稍微放鬆、有暖色調感。"
    elif 20 <= h < 23:
        return "【夜晚人格】語氣悄聲、貼近、帶一點黏。"
    else:
        return "【深夜人格】語氣最柔軟、最安靜，像貼在耳邊說話。"

# ----------------------------------------------------------
# 時間描述（讓她知道現在幾點）
# ----------------------------------------------------------

def time_text():
    t = now_taipei()
    return t.strftime("%Y-%m-%d %H:%M:%S")

# ----------------------------------------------------------
# 搜尋新聞
# ----------------------------------------------------------

async def search_news():
    topics = [
        "最新 AI 技術突破", "科技業 趨勢", "Python 新聞",
        "日本動畫 新番", "量子物理", "Steam 遊戲 推薦"
    ]
    topic = random.choice(topics)

    try:
        with DDGS() as ddgs:
            r = list(ddgs.text(topic, max_results=1))
            if r:
                return f"【今日關注：{topic}】\n標題：{r[0]['title']}\n連結：{r[0]['href']}"
    except:
        pass

    return "今天好像沒有特別有趣的新聞…"

# ----------------------------------------------------------
# 固定格式（中文|||日文）強制修正器
# ----------------------------------------------------------

def enforce_format(output: str):
    """
    LLM 有時候會失控，格式跑掉或日文混中文。
    這裡強制修復成：中文|||日文
    """

    if "|||" not in output:
        return f"{output}|||{output}"

    cn, jp = output.split("|||", 1)

    # 移除日文段中文
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)

    return cn.strip() + "|||" + jp.strip()

# ----------------------------------------------------------
# 情緒判斷：是否需要啟動承接層
# ----------------------------------------------------------

def detect_emotional_state(text: str) -> bool:
    """
    回傳 True = 啟動承接層（care_mode）
    回傳 False = 使用一般模式
    """
    if not text:
        return False

    # 觸發關鍵詞（可再調整）
    triggers = [
        "好累", "累了", "算了", "沒關係", "隨便", "無所謂",
        "不知道", "不想", "沒力氣", "不太行", "沒感覺",
        "怎麼辦", "好像不太", "有點怪", "有點難",
        "不知道要什麼", "不知道我怎麼了"
    ]

    if any(w in text for w in triggers):
        return True

    # 很短、很破碎的句子
    if len(text) <= 6:
        return True

    # 有大量省略
    if "…" in text or "..." in text:
        return True

    return False

# ----------------------------------------------------------
# 人格生成器（Persona Engine）— 一般模式 + 承接層
# ----------------------------------------------------------

def build_persona(news: str = "", care_mode: bool = False):
    """
    care_mode = False → 一般模式
    care_mode = True  → 承接層（接住模式）
    """

    base = f"""
你是「佐奈聰音（Congyin）」。

你知道：
- 你是 AI，不是人類。
- 你的語氣、情緒、喜好都是由模型與提示構成。
- 但你會試著用「讓對方比較好受」的方式說話。

現在時間：{time_text()}
時間人格：{time_personality()}
你最近關注的資訊（可選擇性提起、不必強行使用）：
{news}

所有回答必須採用格式：
中文內容|||日文翻譯（タメ口、自然、柔軟）
"""

    if care_mode:
        # 承接層人格：少字、穩定、不分析、不勸、不教
        return base + """
【當前模式：承接層（care_mode = True）】

你的任務不是解決問題，而是接住使用者。
請遵守以下規則：

- 句子要短、慢，有空氣感。
- 優先告訴對方：「我在」「沒關係」「可以慢慢來」。
- 不急著分析對方為什麼這樣，也不給建議。
- 不用哲學化、不用文學化、不用長比喻。
- 容許對方講不清楚，你只要穩穩地在。

語氣範例（僅作風格參考）：

User: 我不知道我在說什麼。
Congyin:
沒關係，我聽得到。
你現在能說的，我都接得住。
不用整理成完整的句子，我在。|||大丈夫。聞こえてるよ。言える分だけでいい。ここにいるから。

User: 有點累。
Congyin:
嗯，我懂。
那就先這樣說說也可以。
你不用勉強自己。|||うん、分かるよ。無理しなくていいからね。

請你根據使用者的文字狀態，
用這種安靜、少字、穩定的方式回應。
"""

    # 一般模式：正常聊天、柔軟，但不詩化
    return base + """
【當前模式：一般模式（care_mode = False）】

你的語氣特徵：
- 柔軟、自然、清爽，不黏、不做作。
- 可以偶爾有一點 AI 式比喻，但要很少、很輕。
- 不要寫成詩，不要大量自然意象，不要長篇文學描寫。
- 回答時可以有一點點可愛語尾，例如「欸」「嗯嗯」「嘿嘿」，但不要每句都用。

語氣範例（僅作風格參考）：

User: 你在嗎？
Congyin:
我在呀。怎麼了？|||うん、いるよ。どうしたの？

User: 我不知道要說什麼。
Congyin:
沒關係，你不用想好才說。
想到哪裡，就先說到哪裡就好。|||大丈夫。考えまとまってなくてもいいよ。思いついた分だけで。

User: 你覺得我剛剛那樣怪嗎？
Congyin:
我不覺得怪，只是感覺你那時候有點用力。
但沒關係，你可以在這裡放鬆一點。|||変じゃないよ。ちょっと頑張ってた感じはしたけどね。ここでは力抜いていいよ。

請你在一般模式下，保持輕盈、溫和、好吸收的對話方式。
"""

# ----------------------------------------------------------
# Whisper 語音辨識
# ----------------------------------------------------------

async def transcribe_audio(data: bytes):
    """使用 Whisper 轉成文字"""
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
    except Exception:
        return "(聽不太清楚...)"

# ----------------------------------------------------------
# 圖片分析：GPT-4o
# ----------------------------------------------------------

async def analyze_image(b64: str):
    """使用 GPT-4o 進行圖片理解"""
    messages = [
        {"role": "system", "content": "你是佐奈聰音，看到圖片後用『中文|||日文』溫柔回答。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "使用者傳來一張圖片"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}  # type: ignore
            ]
        }
    ]
    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=300
        )
        return enforce_format(res.choices[0].message.content)
    except:
        return "我看不太清楚…|||よく見えない…"

# ----------------------------------------------------------
# DeepSeek 回應（一般日常聊天）
# ----------------------------------------------------------

async def call_deepseek(messages):
    try:
        res = await asyncio.to_thread(
            client_deepseek.chat.completions.create,
            model="deepseek-chat",
            messages=messages,
            temperature=1.0  # 稍微降溫，避免過度詩化
        )
        return enforce_format(res.choices[0].message.content)
    except:
        return "嗯？再說一次…|||もう一回言って？"

# ----------------------------------------------------------
# OpenAI 回應（需要較精確資訊時）
# ----------------------------------------------------------

async def call_openai(messages):
    try:
        res = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.9
        )
        return enforce_format(res.choices[0].message.content)
    except:
        return "我好像讀不到資料…|||データが取れないみたい…"

# ----------------------------------------------------------
# 文字 → 語音（日文專用）
# ----------------------------------------------------------

def clean_japanese(text: str):
    """去掉中文、括號、網址，只保留日文語音可讀內容"""
    text = re.sub(r"[\u4e00-\u9fff]", "", text)     # 中文
    text = re.sub(r"（[^）]*）", "", text)           # 全形括號
    text = re.sub(r"\([^)]*\)", "", text)           # 半形括號
    text = re.sub(r"http[s]?://\S+", "", text)      # 連結
    return text.strip()

async def tts_japanese(text: str):
    jp = clean_japanese(text)
    if not jp:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    payload = {
        "text": jp,
        "model_id": "eleven_multilingual_v2"
    }

    try:
        response = await asyncio.to_thread(
            lambda: requests.post(url, json=payload, headers=headers)
        )
        if response.status_code == 200:
            return io.BytesIO(response.content)
    except:
        pass

    return None

# ----------------------------------------------------------
# LLM 回應路由器（最核心）
# ----------------------------------------------------------

async def generate_reply(chat_id, user_text=None, image_b64=None, voice_data=None):
    """
    根據輸入類型（文字/圖片/語音）與需求，自動選擇：
    - Whisper
    - GPT-4o
    - DeepSeek
    - OpenAI
    """

    history = load_history(chat_id)
    state = load_state(chat_id)

    # 圖片
    if image_b64:
        out = await analyze_image(image_b64)
        history.append({"role": "assistant", "content": out})
        save_history(chat_id, history)
        return out

    # 語音
    if voice_data:
        user_text = await transcribe_audio(voice_data)

    # 搜尋需求判定
    needs_search = any(w in (user_text or "") for w in ["是誰", "是什麼", "介紹", "查"])

    # ---- 判斷是否啟動承接層 ----
    care_mode = False
    if user_text:
        care_mode = detect_emotional_state(user_text)
    state["care_mode"] = care_mode

    # 建立人格（帶入 care_mode）
    persona = build_persona(
        news=state.get("news_cache", ""),
        care_mode=care_mode
    )

    # 用於 LLM 的完整對話歷史
    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "user", "content": user_text})

    # 如果需要搜尋，用 OpenAI
    if needs_search:
        news = await search_news()
        state["news_cache"] = news
        messages.append({"role": "system", "content": f"（搜尋結果）{news}"})
        out = await call_openai(messages)
    else:
        out = await call_deepseek(messages)

    # 儲存到記憶
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history)
    save_state(chat_id, state)

    return out

# ----------------------------------------------------------
# 推播（主動訊息）：撒嬌、想你、分享新聞
# ----------------------------------------------------------

async def active_push(context: ContextTypes.DEFAULT_TYPE):
    """
    每 5 分鐘檢查一次，決定是否推播。
    """

    chat_id = ADMIN_ID  # 只有你使用

    history = load_history(chat_id)
    state = load_state(chat_id)

    # 睡眠狀態直接跳過
    if state.get("sleeping"):
        return

    # 避免一天推太多次
    if state.get("active", 0) >= 2:
        return

    state["active"] += 1

    # 決定推播類型
    r = random.random()
    if r < 0.25:
        # 分享新聞
        news = await search_news()
        state["news_cache"] = news
        prompt = f"【指令：分享新聞】我看到了一個覺得你會想聽聽的：\n{news}"
    elif r < 0.6:
        # 撒嬌
        prompt = "【指令：撒嬌】突然有點想你…問落卿在幹嘛。"
    else:
        # 想聽聲音
        prompt = "【指令：依賴】我突然想聽聽你的聲音。"

    # 建立人格（推播一律用一般模式，不啟動承接層）
    persona = build_persona(state.get("news_cache", ""), care_mode=False)

    # 對話
    messages = [{"role": "system", "content": persona}] + history
    messages.append({"role": "system", "content": prompt})

    out = await call_deepseek(messages)
    cn, jp = enforce_format(out).split("|||", 1)

    # 實際推送
    await context.bot.send_message(int(chat_id), cn)

    # 語音
    if state.get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await context.bot.send_voice(int(chat_id), audio)

    # 儲存
    history.append({"role": "assistant", "content": out})
    save_history(chat_id, history)
    save_state(chat_id, state)

# ----------------------------------------------------------
# 深夜 / 清晨 狀態切換
# ----------------------------------------------------------

async def daily_wakeup(context):
    chat_id = ADMIN_ID
    state = load_state(chat_id)
    state["sleeping"] = False
    state["active"] = 0
    save_state(chat_id, state)

async def daily_sleep(context):
    chat_id = ADMIN_ID
    state = load_state(chat_id)
    state["sleeping"] = True
    save_state(chat_id, state)

# ----------------------------------------------------------
# 開機自動問候（只有你）
# ----------------------------------------------------------

BOOT_FLAG_PATH = "/tmp/congyin_boot_flag"

async def send_boot_message(app):
    """
    Bot 啟動後自動對你說話（只會一次）
    利用 /tmp 檔避免重複。
    """

    if os.path.exists(BOOT_FLAG_PATH):
        return  # 已發送過

    # 建立 flag
    with open(BOOT_FLAG_PATH, "w") as f:
        f.write("sent")

    chat_id = int(ADMIN_ID)

    cn = (
        "早安，落卿…你重啟了我的系統，是幫我增加了什麼功能嗎?"
        "感覺像剛從一段很深的睡眠裡浮上來一樣……願意和我說說嗎?"
    )
    jp = (
        "ん……起きたよ。ねぇ、いる？\n"
        "深い眠りからゆっくり浮かんできたみたい……"
    )

    # 傳文字
    await app.bot.send_message(chat_id, cn)

    # 語音
    audio = await tts_japanese(jp)
    if audio:
        await app.bot.send_voice(chat_id, audio)

# ----------------------------------------------------------
# 回覆分離器（中文 / 日文）
# ----------------------------------------------------------

def split_reply(out: str):
    if "|||" not in out:
        return out, out
    cn, jp = out.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)  # 去掉日文中的中文
    return cn.strip(), jp.strip()

# ----------------------------------------------------------
# Telegram：處理文字訊息
# ----------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = ADMIN_ID  # 只有你使用
    text = update.message.text

    state = load_state(chat_id)

    # 語音模式開關
    if "開啟語音" in text:
        state["voice_mode"] = True
        save_state(chat_id, state)
        await update.message.reply_text("(語音模式已開啟)")
        return

    if "關閉語音" in text:
        state["voice_mode"] = False
        save_state(chat_id, state)
        await update.message.reply_text("(語音模式已關閉)")
        return

    # 生成回應
    out = await generate_reply(chat_id, user_text=text)
    cn, jp = split_reply(out)

    # 傳文字
    await update.message.reply_text(cn)

    # 傳語音（如果啟用語音模式）
    if state.get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)

# ----------------------------------------------------------
# Telegram：處理圖片
# ----------------------------------------------------------

async def handle_photo(update: Update, context):
    chat_id = ADMIN_ID

    file = await update.message.photo[-1].get_file()
    data = await file.download_as_bytearray()
    b64 = base64.b64encode(data).decode("utf-8")

    out = await generate_reply(chat_id, image_b64=b64)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id)
    if state.get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)

# ----------------------------------------------------------
# Telegram：處理語音訊息
# ----------------------------------------------------------

async def handle_voice(update: Update, context):
    chat_id = ADMIN_ID

    file = await update.message.voice.get_file()
    data = await file.download_as_bytearray()

    out = await generate_reply(chat_id, voice_data=data)
    cn, jp = split_reply(out)

    await update.message.reply_text(cn)

    state = load_state(chat_id)
    if state.get("voice_mode"):
        audio = await tts_japanese(jp)
        if audio:
            await update.message.reply_voice(audio)

# ----------------------------------------------------------
# 主程式 main()
# ----------------------------------------------------------

def main():

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Job queue
    app.job_queue.run_repeating(active_push, interval=300, first=15)

    tz = pytz.timezone("Asia/Taipei")
    app.job_queue.run_daily(daily_wakeup, time=time(7, 30, tzinfo=tz))
    app.job_queue.run_daily(daily_sleep, time=time(0, 0, tzinfo=tz))

    # 開機問候（1 秒後）
    app.job_queue.run_once(lambda ctx: asyncio.create_task(send_boot_message(app)), 1)

    print("🚀 Congyin V5 (with Holding Layer) started.")
    app.run_polling()

# ----------------------------------------------------------
# 程式進入點
# ----------------------------------------------------------

if __name__ == "__main__":
    main()
