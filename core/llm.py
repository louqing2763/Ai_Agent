# ==========================================================
# llm.py — 全部語氣行為改為讀取 persona_config
# ==========================================================

import os, re, asyncio, random
from openai import OpenAI

from core.persona_config import (
    SOFTEN_MAP, TAILS, TAIL_CONFIG
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client_openai = OpenAI(api_key=OPENAI_API_KEY)


# ----------------------------------------------------------
# 格式強制「中文|||日文」
# ----------------------------------------------------------

def enforce_format(text):
    if not text:
        return "……|||……"

    text = text.strip()
    if "|||" not in text:
        return f"{text}|||{text}"

    cn, jp = text.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)
    return f"{cn.strip()}|||{jp.strip()}"


# ----------------------------------------------------------
# 語氣柔化（完全由 persona_config 控制）
# ----------------------------------------------------------

def soften_tone(text):
    for k, v in SOFTEN_MAP.items():
        text = text.replace(k, v)
    return text


# ----------------------------------------------------------
# 尾音（affinity 可由 main 控制）
# ----------------------------------------------------------

def apply_tail(cn: str, affinity: float):
    if affinity > TAIL_CONFIG["soft_threshold"]:
        cn += " " + random.choice(TAILS["soft"])
    if affinity > TAIL_CONFIG["lover_threshold"]:
        cn += " " + random.choice(TAILS["lover"])
    return cn


# ----------------------------------------------------------
# OpenAI 呼叫（主引擎）
# ----------------------------------------------------------

async def call_openai(messages, affinity=1.0):

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

    cn = soften_tone(cn)
    cn = apply_tail(cn, affinity)

    return f"{cn}|||{jp.strip()}"
