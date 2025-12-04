import os
import re
import asyncio
from openai import OpenAI

# ---- Load Keys ----
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# ---- Clients ----
client_openai = OpenAI(api_key=OPENAI_API_KEY)

client_deepseek = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

# ---- Format Enforcement ----
def enforce_format(text):
    if "|||" not in text:
        return f"{text}|||{text}"
    cn, jp = text.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)  # remove Chinese accidentally
    return f"{cn.strip()}|||{jp.strip()}"

# ----------------------------------------------------------
# OpenAI async wrapper
# ----------------------------------------------------------
async def call_openai(messages):
    """
    OpenAI 官方支援 async，但為了與 DeepSeek async 一致，
    這裡用 asyncio.to_thread 作為保險，讓兩邊行為一致。
    """
    res = await asyncio.to_thread(
        lambda: client_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.9,
        )
    )
    return enforce_format(res.choices[0].message.content)

# ----------------------------------------------------------
# DeepSeek async wrapper
# ----------------------------------------------------------
async def call_deepseek(messages):
    """
    DeepSeek 官方 SDK 綁在 OpenAI client 上，但是同步 API。
    所以必須使用 asyncio.to_thread 包起來，才能 await。
    """
    res = await asyncio.to_thread(
        lambda: client_deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=1.05,
        )
    )
    return enforce_format(res.choices[0].message.content)
