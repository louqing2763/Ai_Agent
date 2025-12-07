# ==========================================================
#   LLM Wrapper — Congyin V8.4 Mode
# ==========================================================

import os
import re
import asyncio
import random
from openai import OpenAI


# ----------------------------------------------------------
# Load Keys
# ----------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

client_openai = OpenAI(api_key=OPENAI_API_KEY)

client_deepseek = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)


# ==========================================================
# Utility：強制格式「中文|||日文」
# ==========================================================

def enforce_format(text):
    if not text:
        return "……|||……"

    text = text.strip()

    if "|||" not in text:
        # 若模型忘記輸出日文 → 暫時複製
        return f"{text}|||{text}"

    cn, jp = text.split("|||", 1)

    # 去除日文中的中文（GPT 偶爾會混進去）
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)

    return f"{cn.strip()}|||{jp.strip()}"


# ==========================================================
# Meguru Style 語氣調整器（核心）
# ==========================================================

def meguru_tone(cn: str):
    """
    Gamer / 辣妹 / 學妹風：快速、元氣、口語化、帶顏文字。
    不撒嬌、不拖尾、不柔軟。
    """

    # ---- 1. 基礎語氣調整（讓語句更像高中女生） ----
    replace_table = {
        "真的": "真的啦",
        "不是": "不是啦",
        "好吧": "好啦好啦",
        "我覺得": "我在想啦",
        "欸": "欸欸",
        "好像": "好像喔",
        "怎麼辦": "怎麼辦啦",
        "原來如此": "喔喔原來是這樣！",
    }

    for k, v in replace_table.items():
        cn = cn.replace(k, v)

    # ---- 2. 加入 Gamer 氣質（網路梗/顏文字）----
    gamer_terms = [
        "www", "XDD", "笑死", "(≧∇≦)", "(´・ω・`)", "(∠・ω< )⌒☆"
    ]

    # 20% 機率自動加顏文字（Meguru 風核心）
    if random.random() < 0.20:
        cn += " " + random.choice(gamer_terms)

    # ---- 3. Meguru 必備：Ciallo 啟動（5% 機率）----
    if random.random() < 0.05:
        cn = f"Ciallo～(∠・ω< )⌒☆ {cn}"

    return cn


# ==========================================================
# Core — OpenAI Wrapper (Async)
# ==========================================================

async def call_openai(messages, affinity=1.0):
    """
    主要引擎：OpenAI
    這邊不再加入「戀人尾音」或「柔化器」，
    改成 Meguru 專屬語氣調整。
    """

    response = await asyncio.to_thread(
        lambda: client_openai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=1.0,
            messages=messages
        )
    )

    raw = response.choices[0].message.content
    out = enforce_format(raw)

    cn, jp = out.split("|||", 1)

    # ---- 使用 Meguru 風調整器 ----
    cn = meguru_tone(cn)

    return f"{cn}|||{jp.strip()}"


# ==========================================================
# DeepSeek Wrapper（如需切換）
# ==========================================================

async def call_deepseek(messages, affinity=1.0):

    response = await asyncio.to_thread(
        lambda: client_deepseek.chat.completions.create(
            model="deepseek-chat",
            temperature=1.1,
            messages=messages
        )
    )

    raw = response.choices[0].message.content
    out = enforce_format(raw)

    cn, jp = out.split("|||", 1)

    # 同樣使用 Meguru 語氣調整
    cn = meguru_tone(cn)

    return f"{cn}|||{jp.strip()}"
