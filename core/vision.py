# ==========================================================
# vision.py — 使用 OpenAI Responses API 解析圖片
# ==========================================================

import asyncio
from openai import OpenAI

client = OpenAI()

async def analyze_image(b64):
    """使用新版 OpenAI responses API 分析圖片"""

    try:
        res = await asyncio.to_thread(
            client.responses.create,
            model="gpt-4o-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "這張圖片的內容是什麼？"},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{b64}"
                        }
                    ]
                }
            ]
        )

        # 回傳純文字輸出
        return res.choices[0].message.content

    except Exception as e:
        return "我看不太清楚這張圖片呢。"
