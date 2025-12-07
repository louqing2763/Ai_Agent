# ==========================================================
# news.py — 不需要 llm.py，純搜尋新聞摘要
# ==========================================================

import os
import aiohttp
import asyncio

NEWS_API_KEY = os.getenv("NEWS_API_KEY")

# 你也可以留空，反正 main 會處理沒有新聞的情況
async def search_news():
    try:
        url = f"https://newsapi.org/v2/top-headlines?country=tw&apiKey={NEWS_API_KEY}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()

        articles = data.get("articles", [])
        if not articles:
            return "今天沒有什麼特別的新聞喔。"

        # 取前三則
        top = articles[:3]
        summary = "\n".join([f"- {a['title']}" for a in top])

        return f"今日新聞更新：\n{summary}"

    except Exception as e:
        return "目前無法取得最新新聞。"

