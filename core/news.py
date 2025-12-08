# ==========================================================
# news.py — 透過 NewsAPI 取得新聞標題，不使用 llm
# ==========================================================

import os
import aiohttp

NEWS_API_KEY = os.getenv("NEWS_API_KEY")

async def search_news():
    """
    從 NewsAPI 抓取新聞，不依賴 LLM。
    """
    try:
        url = f"https://newsapi.org/v2/top-headlines?country=tw&apiKey={NEWS_API_KEY}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()

        articles = data.get("articles", [])
        if not articles:
            return "目前沒有可用新聞。"

        top = articles[:3]
        summary = "\n".join([f"- {a['title']}" for a in top])

        return f"今日新聞：\n{summary}"

    except Exception:
        return "新聞服務暫時無法取得。"
