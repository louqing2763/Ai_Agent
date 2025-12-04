import random
from duckduckgo_search import DDGS

async def search_news():
    topics = ["AI 最新突破", "科技趨勢", "Python 新聞",
        "日本動畫 新番", "量子物理", "Steam 遊戲"]
    topic = random.choice(topics)
    try:
        with DDGS() as ddgs:
            r = list(ddgs.text(topic, max_results=1))
            if r:
                return f"【今日關注：{topic}】\n{r[0]['title']}\n{r[0]['href']}"
    except:
        return "今天沒有特別的新聞…"
