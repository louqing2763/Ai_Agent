# ==========================================================
# news.py — 無 llm、無 aiohttp、最穩定新聞 API
# ==========================================================

import os
import requests

NEWS_API_KEY = os.getenv("NEWS_API_KEY")

def search_news():
    """以最安全方式取得台灣前三則新聞摘要"""
    if not NEWS_API_KEY:
        return "目前沒有新聞來源設定喔。"

    try:
        url = (
            "https://newsapi.org/v2/top-headlines?"
            "country=tw&"
            f"apiKey={NEWS_API_KEY}"
        )

        resp = requests.get(url, timeout=5)
        data = resp.json()

        articles = data.get("articles", [])
        if not articles:
            return "今天沒有什麼特別的新聞喔。"

        summary = "\n".join([f"- {a.get('title','(無標題)')}" for a in articles[:3]])
        return f"今日新聞更新：\n{summary}"

    except Exception:
        return "目前無法取得最新新聞。"
