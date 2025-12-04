import re
from openai import OpenAI

client_openai = OpenAI()
client_deepseek = OpenAI(base_url="https://api.deepseek.com")

def enforce_format(text):
    if "|||" not in text:
        return f"{text}|||{text}"
    cn, jp = text.split("|||", 1)
    jp = re.sub(r"[\u4e00-\u9fff]", "", jp)
    return f"{cn.strip()}|||{jp.strip()}"

async def call_openai(messages):
    res = await client_openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.9,
    )
    return enforce_format(res.choices[0].message.content)

async def call_deepseek(messages):
    res = await client_deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        temperature=1.05,
    )
    return enforce_format(res.choices[0].message.content)
