import asyncio
from openai import OpenAI

client = OpenAI()

async def analyze_image(b64):
    messages = [
        {"role": "system", "content": "你是佐奈聰音。"},
        {
            "role": "user",
            "content": [
                {"type":"text","text":"這張圖怎麼看？"},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
            ]
        }
    ]
    res = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4o-mini",
        messages=messages
    )
    return res.choices[0].message.content
