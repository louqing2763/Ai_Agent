# ==========================================================
#   LLM Wrapper — Congyin V7.8 Lover Mode
#   for OpenAI / DeepSeek unified behavior
# ==========================================================

import os
import re
import asyncio
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

    # 避免模型輸出換行過多 / 空白段
    text = text.strip()

    # 若沒有 "|||" → 日文 = 中文的翻譯（暫時）
    if "|||" not in text:
        return f"{text}|||{text}"

    cn, jp = text.split("|||", 1)

    # 去除日文中的中文（GPT 偶爾會混到）
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)

    return f"{cn.strip()}|||{jp.strip()}"


# ==========================================================
# 語氣柔化器（避免 GPT 回覆過硬）
# ==========================================================

def soften_tone(text: str):
    """
    讓 GPT 的中文變得更柔、更像聰音。
    不會改變意思，只會調整語氣。
    """

    replacements = {
        "我覺得": "我在想啊…",
        "其實": "其實呢…",
        "不是": "不是啦…",
        "好嗎": "好嗎？",
        "可以": "可以喔",
    }

    for k, v in replacements.items():
        text = text.replace(k, v)

    return text


# ==========================================================
# 恋人尾音調節器（依 affinity 調整甜度）
# ==========================================================

def apply_lover_tail(cn: str, affinity: float):
    """
    尾音僅在中文加入，保持日文乾淨。
    affinity < 1.0 → 幾乎無尾音
    affinity > 1.3 → 柔甜
    affinity > 1.5 → 偶爾撒嬌
    """

    tails_soft = ["…嗯", "…好嘛", "…嘿嘿", "…唔"]
    tails_lover = ["…好想你", "…陪我一下嘛", "…可以抱你嗎？"]
    
    # 小甜味
    if affinity > 1.3:
        cn += " " + tails_soft[int(affinity * 10) % len(tails_soft)]

    # 撒嬌味（但不過度）
    if affinity > 1.5 and len(cn) < 55:
        if random.random() < 0.25:
            cn += " " + tails_lover[int(affinity * 7) % len(tails_lover)]

    return cn


# ==========================================================
# Core — OpenAI Wrapper (Async)
# ==========================================================

async def call_openai(messages, affinity=1.0):
    """
    對 OpenAI 發送請求，並注入語氣變換器。
    """

    res = await asyncio.to_thread(
        lambda: client_openai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.9,
            messages=messages
        )
    )

    raw = res.choices[0].message.content
    out = enforce_format(raw)

    cn, jp = out.split("|||", 1)

    # 語氣柔化 + 恋人尾音
    cn = soften_tone(cn)
    cn = apply_lover_tail(cn, affinity)

    return f"{cn}|||{jp.strip()}"


# ==========================================================
# DeepSeek Wrapper（如需切換回 DS 用）
# ==========================================================

async def call_deepseek(messages, affinity=1.0):

    res = await asyncio.to_thread(
        lambda: client_deepseek.chat.completions.create(
            model="deepseek-chat",
            temperature=1.05,
            messages=messages
        )
    )

    raw = res.choices[0].message.content
    out = enforce_format(raw)

    cn, jp = out.split("|||", 1)

    # DeepSeek 回覆更需要柔化
    cn = soften_tone(cn)
    cn = apply_lover_tail(cn, affinity)

    return f"{cn}|||{jp.strip()}"
