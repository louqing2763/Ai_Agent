import os
import re
from openai import OpenAI

# ----------------------------------------------------------
# 讀環境變數裡的金鑰（跟 main.py 同一組）
# ----------------------------------------------------------

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 安全檢查（可選，如果沒設會直接拋錯，比較好 Debug）
if not OPENAI_API_KEY:
    print("[WARN] OPENAI_API_KEY is not set in environment")

if not DEEPSEEK_API_KEY:
    print("[WARN] DEEPSEEK_API_KEY is not set in environment")

# ----------------------------------------------------------
# LLM Clients
# ----------------------------------------------------------

# OpenAI：走官方 gpt-4o-mini
client_openai = OpenAI(
    api_key=OPENAI_API_KEY,
)

# DeepSeek：走 deepseek-chat，重點是這裡的 api_key
client_deepseek = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

# ----------------------------------------------------------
# 格式修正：中文 ||| 日文
# ----------------------------------------------------------

def enforce_format(text: str) -> str:
    if "|||" not in text:
        return f"{text}|||{text}"
    cn, jp = text.split("|||", 1)
    # 把日文段裡不小心混進去的中文去掉
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)
    return f"{cn.strip()}|||{jp.strip()}"

# ----------------------------------------------------------
# OpenAI 路由
# ----------------------------------------------------------

async def call_openai(messages):
    """
    一般用在：需要比較穩、或要查資料（搭配 search_news）
    """
    res = await client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.9,
    )
    return enforce_format(res.choices[0].message.content)

# ----------------------------------------------------------
# DeepSeek 路由（主聊天用）
# ----------------------------------------------------------

async def call_deepseek(messages):
    """
    主要對話用，走 deepseek-chat
    """
    res = await client_deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        temperature=1.05,
    )
    return enforce_format(res.choices[0].message.content)
