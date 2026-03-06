"""
tests/test_news.py — Unit tests for core/news.py

Coverage areas:
- _match_rss_key(): keyword routing logic
- _format_results(): output formatting and HTML stripping
- clear_news_cache(): cache management
- search_news(): full pipeline with mocked external calls (cache hit, DDG path, RSS fallback)
"""

import time
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.news import _match_rss_key, _format_results, clear_news_cache, search_news, _cache, CACHE_TTL


# ------------------------------------------------------------------
# _match_rss_key
# ------------------------------------------------------------------
class TestMatchRssKey:
    def test_tech_keywords_english(self):
        assert _match_rss_key("ai news today") == "科技"
        assert _match_rss_key("tech startup funding") == "科技"
        assert _match_rss_key("apple google chip") == "科技"

    def test_tech_keywords_chinese(self):
        assert _match_rss_key("科技新聞") == "科技"
        assert _match_rss_key("蘋果發布新產品") == "科技"
        assert _match_rss_key("晶片供應鏈") == "科技"

    def test_finance_keywords(self):
        assert _match_rss_key("財經新聞") == "財經"
        assert _match_rss_key("股票市場今日走勢") == "財經"
        assert _match_rss_key("投資報酬率") == "財經"
        assert _match_rss_key("匯率變動") == "財經"
        assert _match_rss_key("經濟衰退") == "財經"

    def test_world_keywords(self):
        assert _match_rss_key("世界新聞") == "世界"
        assert _match_rss_key("國際局勢") == "世界"
        assert _match_rss_key("美國總統選舉") == "世界"
        assert _match_rss_key("歐洲能源危機") == "世界"
        assert _match_rss_key("聯合國決議") == "世界"

    def test_default_fallback(self):
        assert _match_rss_key("") == "台灣"
        assert _match_rss_key("today's headline") == "台灣"
        assert _match_rss_key("random query") == "台灣"

    def test_case_insensitive(self):
        assert _match_rss_key("AI Technology") == "科技"
        assert _match_rss_key("TECH news") == "科技"


# ------------------------------------------------------------------
# _format_results
# ------------------------------------------------------------------
class TestFormatResults:
    def test_empty_list_returns_empty_string(self):
        assert _format_results([]) == ""

    def test_single_item_with_title_and_body(self):
        items = [{"title": "Big News", "body": "Something happened today."}]
        result = _format_results(items)
        assert "1. Big News｜Something happened today." in result

    def test_source_label_appears_first(self):
        items = [{"title": "Headline", "body": "Details here."}]
        result = _format_results(items, source_label="DuckDuckGo")
        lines = result.split("\n")
        assert lines[0] == "[來源: DuckDuckGo]"

    def test_html_tags_stripped_from_body(self):
        items = [{"title": "Title", "body": "<p>Clean <b>text</b> here.</p>"}]
        result = _format_results(items)
        assert "<p>" not in result
        assert "<b>" not in result
        assert "Clean text here." in result

    def test_body_truncated_at_180_chars(self):
        long_body = "A" * 200
        items = [{"title": "Title", "body": long_body}]
        result = _format_results(items)
        assert "…" in result
        # Body portion should not exceed 180 chars + ellipsis
        body_part = result.split("｜")[1]
        assert len(body_part) <= 184  # 180 + "…" + some margin

    def test_item_with_only_title_no_body(self):
        items = [{"title": "Just a title", "body": ""}]
        result = _format_results(items)
        assert "Just a title" in result
        assert "｜" not in result

    def test_item_without_title_skipped(self):
        items = [{"title": "", "body": "Body without title"}]
        result = _format_results(items)
        # Items with no title should not appear (title is required)
        assert "Body without title" not in result

    def test_multiple_items_numbered(self):
        items = [
            {"title": "First", "body": "First body"},
            {"title": "Second", "body": "Second body"},
            {"title": "Third", "body": "Third body"},
        ]
        result = _format_results(items)
        assert "1. First" in result
        assert "2. Second" in result
        assert "3. Third" in result


# ------------------------------------------------------------------
# clear_news_cache
# ------------------------------------------------------------------
class TestClearNewsCache:
    def setup_method(self):
        """Seed the cache before each test."""
        from core import news as news_mod
        news_mod._cache["test_key"] = (time.time(), "cached result")
        news_mod._cache["other_key"] = (time.time(), "other result")

    def teardown_method(self):
        clear_news_cache()

    def test_clear_specific_key(self):
        from core import news as news_mod
        clear_news_cache("test_key")
        assert "test_key" not in news_mod._cache
        assert "other_key" in news_mod._cache

    def test_clear_all_keys(self):
        from core import news as news_mod
        clear_news_cache()
        assert len(news_mod._cache) == 0

    def test_clear_nonexistent_key_no_error(self):
        clear_news_cache("nonexistent_key")  # Should not raise


# ------------------------------------------------------------------
# search_news (mocked external calls)
# ------------------------------------------------------------------
class TestSearchNews:
    def teardown_method(self):
        clear_news_cache()

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_result(self):
        from core import news as news_mod
        # Pre-populate cache
        news_mod._cache["台灣新聞"] = (time.time(), "cached content")
        result = await search_news("台灣新聞")
        assert result == "cached content"

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_new_fetch(self):
        from core import news as news_mod
        # Set an expired cache entry
        news_mod._cache["expired_query"] = (time.time() - CACHE_TTL - 1, "old result")

        ddg_items = [{"title": "Fresh News", "body": "Fresh content"}]
        with patch("core.news._search_ddg", new=AsyncMock(return_value=ddg_items)):
            result = await search_news("expired_query")
        assert "Fresh News" in result
        assert result != "old result"

    @pytest.mark.asyncio
    async def test_ddg_success_returns_formatted_results(self):
        ddg_items = [
            {"title": "Tech Story", "body": "A great tech story", "href": "http://example.com"},
        ]
        with patch("core.news._search_ddg", new=AsyncMock(return_value=ddg_items)):
            result = await search_news("tech news")
        assert "Tech Story" in result
        assert "A great tech story" in result

    @pytest.mark.asyncio
    async def test_ddg_failure_falls_back_to_rss(self):
        rss_items = [{"title": "RSS Headline", "body": "RSS body text"}]
        with patch("core.news._search_ddg", new=AsyncMock(return_value=[])), \
             patch("core.news._fetch_rss", new=AsyncMock(return_value=rss_items)):
            result = await search_news("some query")
        assert "RSS Headline" in result

    @pytest.mark.asyncio
    async def test_empty_query_skips_ddg_goes_to_rss(self):
        rss_items = [{"title": "Top Headline", "body": "Breaking news"}]
        with patch("core.news._fetch_rss", new=AsyncMock(return_value=rss_items)) as mock_rss, \
             patch("core.news._search_ddg", new=AsyncMock(return_value=[])) as mock_ddg:
            result = await search_news("")
        # DDG should not be called for empty query
        mock_ddg.assert_not_called()
        assert "Top Headline" in result

    @pytest.mark.asyncio
    async def test_all_sources_fail_returns_empty_string(self):
        with patch("core.news._search_ddg", new=AsyncMock(return_value=[])), \
             patch("core.news._fetch_rss", new=AsyncMock(return_value=[])):
            result = await search_news("impossible query")
        assert result == ""

    @pytest.mark.asyncio
    async def test_successful_result_is_cached(self):
        from core import news as news_mod
        ddg_items = [{"title": "Cache Me", "body": "This should be cached"}]
        query = "cache test query"
        with patch("core.news._search_ddg", new=AsyncMock(return_value=ddg_items)):
            await search_news(query)
        cache_key = query.strip().lower()
        assert cache_key in news_mod._cache
        _, cached_result = news_mod._cache[cache_key]
        assert "Cache Me" in cached_result

    @pytest.mark.asyncio
    async def test_headline_cache_key_for_empty_query(self):
        from core import news as news_mod
        rss_items = [{"title": "Headline", "body": "News body"}]
        with patch("core.news._fetch_rss", new=AsyncMock(return_value=rss_items)):
            await search_news("")
        assert "__headline__" in news_mod._cache
