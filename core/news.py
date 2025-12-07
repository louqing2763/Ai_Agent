# ==========================================================
# news.py — async 版本，與 main 完全相容
# ==========================================================

import os
import aiohttp

NEWS_API_KEY = os.getenv("NEWS_API_KEY")

async def search_news():
    try:
        if not NEWS_API_KEY:
            return "目前沒有設定新聞 API KEY。"

        url = f"https://newsapi.org/v2/top-headlines?country=tw&apiKey={NEWS_API_KEY}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()

        articles = data.get("articles", [])
        if not articles:
            return "今天沒有什麼特別的新聞喔。"

        # 取前 3 則
        top = articles[:3]
        summary = "\n".join([f"- {a.get('title', '')}" for a in top])

        return f"今日新聞更新：\n{summary}"

    except Exception:
        return "目前無法取得最新新聞。"
