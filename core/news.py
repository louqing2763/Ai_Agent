"""
core/news.py — 莉莉絲新聞模組 v2.1 (NewsAPI 主力版)

支援：
  - DuckDuckGo 即時搜尋（無需 API Key）
  - RSS 訂閱源解析（備用 / 補充）
  - 結果快取（避免短時間內重複請求）

對外暴露的唯一入口：
  await search_news(query: str = "") -> str

安裝依賴：
  pip install duckduckgo-search feedparser
"""

import asyncio
import logging
import time
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# ⚙️ 快取設定
# ----------------------------------------------------------
_cache: dict = {}       # { cache_key: (timestamp, result_str) }
CACHE_TTL = 600         # 10 分鐘內同一關鍵字不重複請求


# ----------------------------------------------------------
# 🦆 DuckDuckGo 即時搜尋（主力，無需 API Key）
# ----------------------------------------------------------
async def _search_ddg(query: str, max_results: int = 5) -> list:
    """
    透過 duckduckgo_search 套件進行全網即時搜尋。
    回傳格式：[{"title": str, "body": str, "href": str}]
    """
    try:
        from duckduckgo_search import DDGS

        def _sync():
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(
                    query,
                    region="zh-tw",
                    safesearch="moderate",
                    max_results=max_results,
                ):
                    results.append(r)
            return results

        return await asyncio.to_thread(_sync)

    except ImportError:
        logger.warning("duckduckgo_search 未安裝。請執行：pip install duckduckgo-search")
        return []
    except Exception as e:
        logger.error(f"DDG 搜尋失敗: {e}")
        return []


# ----------------------------------------------------------
# 📡 RSS 備用源
# ----------------------------------------------------------
RSS_FEEDS = {
    "台灣":  "https://www.cna.com.tw/rss/aall.aspx",
    "科技":  "https://feeds.feedburner.com/techcrunch/startups",
    "世界":  "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "財經":  "https://feeds.bloomberg.com/markets/news.rss",
}
DEFAULT_RSS_KEY = "台灣"


def _match_rss_key(query: str) -> str:
    """根據關鍵字挑選最接近的 RSS 源"""
    q = query.lower()
    if any(k in q for k in ["科技", "ai", "tech", "蘋果", "google", "晶片"]):
        return "科技"
    if any(k in q for k in ["財經", "股票", "市場", "投資", "經濟", "匯率"]):
        return "財經"
    if any(k in q for k in ["世界", "國際", "美國", "歐洲", "戰爭", "聯合國"]):
        return "世界"
    return DEFAULT_RSS_KEY


async def _fetch_rss(feed_url: str, max_items: int = 5) -> list:
    """
    解析 RSS/Atom feed，回傳文章列表。
    依賴：pip install feedparser
    """
    try:
        import feedparser

        def _parse():
            feed = feedparser.parse(feed_url)
            items = []
            for entry in feed.entries[:max_items]:
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                summary = re.sub(r"<[^>]+>", "", summary)   # 移除 HTML 標籤
                summary = summary[:200] + "…" if len(summary) > 200 else summary
                link    = entry.get("link", "")
                if title:
                    items.append({"title": title, "body": summary, "href": link})
            return items

        return await asyncio.to_thread(_parse)

    except ImportError:
        logger.warning("feedparser 未安裝。請執行：pip install feedparser")
        return []
    except Exception as e:
        logger.error(f"RSS 解析失敗 ({feed_url}): {e}")
        return []


# ----------------------------------------------------------
# 🔧 結果格式化
# ----------------------------------------------------------
def _format_results(items: list, source_label: str = "") -> str:
    """
    將搜尋結果壓縮為莉莉絲 prompt 可用的純文字摘要。
    刻意保持簡短，避免 token 浪費。
    """
    if not items:
        return ""

    lines = []
    if source_label:
        lines.append(f"[來源: {source_label}]")

    for i, item in enumerate(items, 1):
        title = item.get("title", "").strip()
        body  = re.sub(r"<[^>]+>", "", item.get("body", "")).strip()
        body  = body[:180] + "…" if len(body) > 180 else body

        if title and body:
            lines.append(f"{i}. {title}｜{body}")
        elif title:
            lines.append(f"{i}. {title}")

    return "\n".join(lines)


# ----------------------------------------------------------
# 🌐 公開入口
# ----------------------------------------------------------
async def search_news(query: str = "") -> str:
    """
    主要呼叫入口。

    流程：
      1. 檢查快取（10 分鐘內同樣 query 直接回傳）
      2. 有 query → 嘗試 DuckDuckGo
      3. DDG 無結果 / 無 query → 走 RSS 備用源
      4. 全部失敗 → 回傳空字串

    Args:
        query: 搜尋關鍵字。空字串代表「取最新頭條」。

    Returns:
        格式化後的新聞摘要字串；失敗時回傳空字串。
    """
    cache_key = query.strip().lower() or "__headline__"

    # 1. 快取命中
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            logger.info(f"[news] 快取命中: '{cache_key}'")
            return cached

    result = ""

    # 2. DuckDuckGo（有 query 時優先）
    if query.strip():
        items = await _search_ddg(query)
        if items:
            result = _format_results(items, source_label=f"即時搜尋：{query}")

    # 3. RSS 備用
    if not result:
        rss_key  = _match_rss_key(query) if query else DEFAULT_RSS_KEY
        feed_url = RSS_FEEDS.get(rss_key, RSS_FEEDS[DEFAULT_RSS_KEY])
        items    = await _fetch_rss(feed_url)
        if items:
            result = _format_results(items, source_label=f"RSS·{rss_key}")

    # 4. 全部失敗
    if not result:
        logger.warning(f"[news] 所有來源均失敗，query='{query}'")
        return ""

    # 5. 寫入快取
    _cache[cache_key] = (time.time(), result)
    logger.info(f"[news] 搜尋成功，query='{query}'，長度={len(result)}")
    return result


# ----------------------------------------------------------
# 🧹 快取工具（供外部呼叫）
# ----------------------------------------------------------
def clear_news_cache(query: Optional[str] = None) -> None:
    """
    清除快取。
    query=None → 清除全部；否則清除指定 key。
    """
    global _cache
    if query is None:
        _cache = {}
        logger.info("[news] 快取已全部清除")
    else:
        key = query.strip().lower()
        _cache.pop(key, None)
        logger.info(f"[news] 快取已清除: '{key}'")
