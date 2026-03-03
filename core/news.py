import os
import time
import asyncio
import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# ----------------------------------------------------------
# ⚙️ 快取（10 分鐘內同一 query 不重複打 API）
# ----------------------------------------------------------
_cache: dict = {}
CACHE_TTL = 600


# ----------------------------------------------------------
# 📰 NewsAPI — 主力引擎
# ----------------------------------------------------------
_CATEGORY_MAP = {
    "科技": "technology", "tech": "technology", "ai": "technology",
    "科學": "science",
    "財經": "business",   "股票": "business",   "投資": "business",
    "健康": "health",
    "娛樂": "entertainment",
    "體育": "sports",     "運動": "sports",
}

def _map_category(query: str) -> Optional[str]:
    q = query.lower()
    for kw, cat in _CATEGORY_MAP.items():
        if kw in q:
            return cat
    return None


async def _fetch_newsapi(query: str = "", max_items: int = 5) -> list:
    """
    有 query → /v2/everything 全文搜尋（zh 語言）
    無 query 或搜不到 → /v2/top-headlines 台灣頭條
    """
    if not NEWS_API_KEY:
        return []

    results = []

    async with aiohttp.ClientSession() as session:

        # 有 query：全文搜尋
        if query.strip():
            params = {
                "q":        query,
                "language": "zh",
                "sortBy":   "publishedAt",
                "pageSize": max_items,
                "apiKey":   NEWS_API_KEY,
            }
            try:
                async with session.get(
                    "https://newsapi.org/v2/everything",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                results = _parse_articles(data.get("articles", []), max_items)
            except Exception as e:
                logger.error(f"[NewsAPI/everything] {e}")

        # 無結果 / 無 query → 台灣頭條
        if not results:
            params = {
                "country":  "tw",
                "pageSize": max_items,
                "apiKey":   NEWS_API_KEY,
            }
            cat = _map_category(query)
            if cat:
                params["category"] = cat
            try:
                async with session.get(
                    "https://newsapi.org/v2/top-headlines",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                results = _parse_articles(data.get("articles", []), max_items)
            except Exception as e:
                logger.error(f"[NewsAPI/top-headlines] {e}")

    return results


def _parse_articles(articles: list, max_items: int) -> list:
    """NewsAPI articles → 統一格式"""
    items = []
    for a in articles[:max_items]:
        title = (a.get("title") or "").strip()
        title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()  # 去掉「- 來源名」
        body  = (a.get("description") or a.get("content") or "").strip()
        body  = re.sub(r"<[^>]+>", "", body)
        body  = body[:180] + "…" if len(body) > 180 else body
        if title:
            items.append({"title": title, "body": body, "href": a.get("url", "")})
    return items


# ----------------------------------------------------------
# 🦆 DuckDuckGo — 第一備用
# ----------------------------------------------------------
async def _search_ddg(query: str, max_results: int = 5) -> list:
    try:
        from duckduckgo_search import DDGS
        def _sync():
            with DDGS() as ddgs:
                return list(ddgs.text(
                    query, region="zh-tw",
                    safesearch="moderate", max_results=max_results
                ))
        return await asyncio.to_thread(_sync)
    except ImportError:
        logger.warning("[DDG] 未安裝：pip install duckduckgo-search")
        return []
    except Exception as e:
        logger.error(f"[DDG] {e}")
        return []


# ----------------------------------------------------------
# 📡 RSS — 最終備用
# ----------------------------------------------------------
RSS_FEEDS = {
    "台灣": "https://www.cna.com.tw/rss/aall.aspx",
    "科技": "https://feeds.feedburner.com/techcrunch/startups",
    "世界": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "財經": "https://feeds.bloomberg.com/markets/news.rss",
}
DEFAULT_RSS_KEY = "台灣"

def _match_rss_key(query: str) -> str:
    q = query.lower()
    if any(k in q for k in ["科技", "ai", "tech", "蘋果", "google"]): return "科技"
    if any(k in q for k in ["財經", "股票", "市場", "投資"]):          return "財經"
    if any(k in q for k in ["世界", "國際", "美國", "歐洲"]):          return "世界"
    return DEFAULT_RSS_KEY

async def _fetch_rss(feed_url: str, max_items: int = 5) -> list:
    try:
        import feedparser
        def _parse():
            feed = feedparser.parse(feed_url)
            items = []
            for e in feed.entries[:max_items]:
                title   = (e.get("title") or "").strip()
                summary = re.sub(r"<[^>]+>", "",
                    e.get("summary", e.get("description", ""))).strip()
                summary = summary[:180] + "…" if len(summary) > 180 else summary
                if title:
                    items.append({
                        "title": title, "body": summary,
                        "href": e.get("link", "")
                    })
            return items
        return await asyncio.to_thread(_parse)
    except ImportError:
        logger.warning("[RSS] 未安裝：pip install feedparser")
        return []
    except Exception as e:
        logger.error(f"[RSS] {e}")
        return []


# ----------------------------------------------------------
# 🔧 格式化（輸出盡量省 token）
# ----------------------------------------------------------
def _format_results(items: list, source_label: str = "") -> str:
    if not items:
        return ""
    lines = [f"[來源: {source_label}]"] if source_label else []
    for i, item in enumerate(items, 1):
        title = item.get("title", "").strip()
        body  = re.sub(r"<[^>]+>", "", item.get("body", "")).strip()
        body  = body[:180] + "…" if len(body) > 180 else body
        lines.append(f"{i}. {title}｜{body}" if body else f"{i}. {title}")
    return "\n".join(lines)


# ----------------------------------------------------------
# 🌐 公開入口（與原版 search_news() 完全相容）
# ----------------------------------------------------------
async def search_news(query: str = "") -> str:
    """
    主要呼叫入口。原版只有 search_news()，這裡保持相同簽名。

    流程：
      1. 快取命中 → 直接回傳
      2. NewsAPI（有 KEY）
      3. DuckDuckGo（有 query）
      4. RSS
      5. 全部失敗 → 空字串

    Args:
        query: 搜尋關鍵字，空字串 = 台灣最新頭條

    Returns:
        格式化新聞摘要；失敗時回傳空字串
    """
    cache_key = query.strip().lower() or "__headline__"

    # 1. 快取
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            logger.info(f"[news] 快取命中: '{cache_key}'")
            return cached

    result = ""

    # 2. NewsAPI（主力）
    if NEWS_API_KEY:
        items = await _fetch_newsapi(query)
        if items:
            label  = f"NewsAPI·{query}" if query else "NewsAPI·台灣頭條"
            result = _format_results(items, source_label=label)
            logger.info(f"[news] NewsAPI 成功，query='{query}'")

    # 3. DDG（第一備用，有 query 才搜）
    if not result and query.strip():
        items = await _search_ddg(query)
        if items:
            result = _format_results(items, source_label=f"DDG·{query}")
            logger.info(f"[news] DDG 備用成功")

    # 4. RSS（最終備用）
    if not result:
        rss_key  = _match_rss_key(query) if query else DEFAULT_RSS_KEY
        feed_url = RSS_FEEDS.get(rss_key, RSS_FEEDS[DEFAULT_RSS_KEY])
        items    = await _fetch_rss(feed_url)
        if items:
            result = _format_results(items, source_label=f"RSS·{rss_key}")
            logger.info(f"[news] RSS 備用成功")

    # 5. 全部失敗
    if not result:
        logger.warning(f"[news] 所有來源失敗，query='{query}'")
        return ""

    _cache[cache_key] = (time.time(), result)
    return result


# ----------------------------------------------------------
# 🧹 快取工具（供 main.py 的 /clearnews 指令呼叫）
# ----------------------------------------------------------
def clear_news_cache(query: Optional[str] = None) -> None:
    global _cache
    if query is None:
        _cache = {}
        logger.info("[news] 快取已全部清除")
    else:
        _cache.pop(query.strip().lower(), None)
        logger.info(f"[news] 快取已清除: '{query}'")
