"""
tests/test_brain.py — Unit tests for agent/brain.py

Coverage areas:
- TOOL_DEFINITIONS: structure validation
- _execute_tool(): routing to correct module for each known tool,
                   handling unknown tool names, and exception safety
- _call_api(): success (200), HTTP error, network exception
- think(): pure text reply path, tool-call path, API failure path,
           tools_enabled=False path, length_mode → max_tokens mapping
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import (
    TOOL_DEFINITIONS,
    _execute_tool,
    _call_api,
    think,
)


# ------------------------------------------------------------------
# TOOL_DEFINITIONS structure
# ------------------------------------------------------------------
class TestToolDefinitions:
    def test_three_tools_defined(self):
        assert len(TOOL_DEFINITIONS) == 3

    def test_all_tools_have_type_function(self):
        for td in TOOL_DEFINITIONS:
            assert td["type"] == "function"

    def test_tool_names_are_correct(self):
        names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        assert names == {"search_news", "get_system_status", "get_weather"}

    def test_each_tool_has_description(self):
        for td in TOOL_DEFINITIONS:
            assert len(td["function"]["description"]) > 0

    def test_each_tool_has_parameters_block(self):
        for td in TOOL_DEFINITIONS:
            assert "parameters" in td["function"]
            assert td["function"]["parameters"]["type"] == "object"


# ------------------------------------------------------------------
# _execute_tool
# ------------------------------------------------------------------
class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_search_news_tool_dispatched(self):
        with patch("core.news.search_news", new=AsyncMock(return_value="News result")) as mock_news:
            result = await _execute_tool("search_news", {"query": "台灣"})
        assert result == "News result"

    @pytest.mark.asyncio
    async def test_search_news_empty_result_returns_fallback(self):
        with patch("core.news.search_news", new=AsyncMock(return_value="")):
            result = await _execute_tool("search_news", {"query": "台灣"})
        assert result == "沒有找到相關新聞。"

    @pytest.mark.asyncio
    async def test_search_news_default_empty_query(self):
        with patch("core.news.search_news", new=AsyncMock(return_value="headlines")) as mock_news:
            await _execute_tool("search_news", {})
        mock_news.assert_called_once_with("")

    @pytest.mark.asyncio
    async def test_get_system_status_tool_dispatched(self):
        with patch("tools.system_monitor.get_system_status", new=AsyncMock(return_value="CPU: 30%")):
            result = await _execute_tool("get_system_status", {"detail": "cpu"})
        assert result == "CPU: 30%"

    @pytest.mark.asyncio
    async def test_get_system_status_empty_result_returns_fallback(self):
        with patch("tools.system_monitor.get_system_status", new=AsyncMock(return_value="")):
            result = await _execute_tool("get_system_status", {})
        assert result == "系統狀態暫時無法取得。"

    @pytest.mark.asyncio
    async def test_get_system_status_default_detail_all(self):
        with patch("tools.system_monitor.get_system_status", new=AsyncMock(return_value="info")) as mock_sys:
            await _execute_tool("get_system_status", {})
        mock_sys.assert_called_once_with("all")

    @pytest.mark.asyncio
    async def test_get_weather_tool_dispatched(self):
        with patch("tools.weather.get_weather", new=AsyncMock(return_value="晴天 28°C")):
            result = await _execute_tool("get_weather", {"city": "台北"})
        assert result == "晴天 28°C"

    @pytest.mark.asyncio
    async def test_get_weather_empty_result_returns_fallback(self):
        with patch("tools.weather.get_weather", new=AsyncMock(return_value="")):
            result = await _execute_tool("get_weather", {"city": "台北"})
        assert result == "天氣資訊暫時無法取得。"

    @pytest.mark.asyncio
    async def test_get_weather_default_city_taipei(self):
        with patch("tools.weather.get_weather", new=AsyncMock(return_value="weather")) as mock_weather:
            await _execute_tool("get_weather", {})
        mock_weather.assert_called_once_with("台北")

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_message(self):
        result = await _execute_tool("nonexistent_tool", {})
        assert "未知工具" in result
        assert "nonexistent_tool" in result

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_message(self):
        with patch("core.news.search_news", new=AsyncMock(side_effect=Exception("crash"))):
            result = await _execute_tool("search_news", {"query": "test"})
        assert "工具執行時發生錯誤" in result


# ------------------------------------------------------------------
# _call_api
# ------------------------------------------------------------------
class TestCallApi:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_json(self):
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"choices": [{"message": {"content": "hi"}}]}

        with patch("requests.post", return_value=fake_response):
            result = await _call_api({"model": "deepseek-chat", "messages": []})

        assert result is not None
        assert result["choices"][0]["message"]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        fake_response = MagicMock()
        fake_response.status_code = 401
        fake_response.text = "Unauthorized"

        with patch("requests.post", return_value=fake_response):
            result = await _call_api({"model": "deepseek-chat", "messages": []})

        assert result is None

    @pytest.mark.asyncio
    async def test_network_exception_returns_none(self):
        with patch("requests.post", side_effect=Exception("connection refused")):
            result = await _call_api({"model": "deepseek-chat", "messages": []})

        assert result is None


# ------------------------------------------------------------------
# think — main reasoning function
# ------------------------------------------------------------------
def _make_text_reply(content: str) -> dict:
    """Build a minimal API response with a plain text reply."""
    return {
        "choices": [{
            "message": {
                "content": content,
                "tool_calls": None,
            }
        }]
    }


def _make_tool_call_reply(tool_name: str, args: dict, tc_id: str = "tc_001") -> dict:
    """Build an API response that requests a tool call."""
    return {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": tc_id,
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args),
                    }
                }]
            }
        }]
    }


class TestThink:
    @pytest.mark.asyncio
    async def test_plain_text_reply_returned_directly(self):
        messages = [{"role": "user", "content": "Hello"}]
        api_response = _make_text_reply("Hi there!")

        with patch("agent.brain._call_api", new=AsyncMock(return_value=api_response)):
            reply, tool_log = await think(messages)

        assert reply == "Hi there!"
        assert tool_log == []

    @pytest.mark.asyncio
    async def test_api_failure_returns_connection_error_message(self):
        messages = [{"role": "user", "content": "Hello"}]

        with patch("agent.brain._call_api", new=AsyncMock(return_value=None)):
            reply, tool_log = await think(messages)

        assert "連線中斷" in reply

    @pytest.mark.asyncio
    async def test_tool_call_path_returns_final_reply(self):
        messages = [{"role": "user", "content": "What's the weather?"}]
        first_response = _make_tool_call_reply("get_weather", {"city": "台北"})
        second_response = _make_text_reply("今天台北天氣晴，28°C。")

        with patch("agent.brain._call_api", new=AsyncMock(side_effect=[first_response, second_response])), \
             patch("agent.brain._execute_tool", new=AsyncMock(return_value="晴天 28°C")):
            reply, tool_log = await think(messages)

        assert reply == "今天台北天氣晴，28°C。"
        assert len(tool_log) == 1
        assert tool_log[0]["tool"] == "get_weather"

    @pytest.mark.asyncio
    async def test_tool_call_second_api_failure_returns_partial_error(self):
        messages = [{"role": "user", "content": "weather?"}]
        first_response = _make_tool_call_reply("get_weather", {"city": "台北"})

        with patch("agent.brain._call_api", new=AsyncMock(side_effect=[first_response, None])), \
             patch("agent.brain._execute_tool", new=AsyncMock(return_value="晴天")):
            reply, tool_log = await think(messages)

        assert "工具執行完畢" in reply or "錯誤" in reply

    @pytest.mark.asyncio
    async def test_tools_disabled_no_tools_in_payload(self):
        messages = [{"role": "user", "content": "Hello"}]
        captured_payloads = []

        async def capture_api(payload):
            captured_payloads.append(payload)
            return _make_text_reply("Response without tools")

        with patch("agent.brain._call_api", new=capture_api):
            await think(messages, tools_enabled=False)

        assert "tools" not in captured_payloads[0]
        assert "tool_choice" not in captured_payloads[0]

    @pytest.mark.asyncio
    async def test_length_mode_short_sets_150_tokens(self):
        messages = [{"role": "user", "content": "Hi"}]
        captured = []

        async def capture_api(payload):
            captured.append(payload)
            return _make_text_reply("ok")

        with patch("agent.brain._call_api", new=capture_api):
            await think(messages, length_mode="short")

        assert captured[0]["max_tokens"] == 150

    @pytest.mark.asyncio
    async def test_length_mode_normal_sets_600_tokens(self):
        messages = [{"role": "user", "content": "Hi"}]
        captured = []

        async def capture_api(payload):
            captured.append(payload)
            return _make_text_reply("ok")

        with patch("agent.brain._call_api", new=capture_api):
            await think(messages, length_mode="normal")

        assert captured[0]["max_tokens"] == 600

    @pytest.mark.asyncio
    async def test_length_mode_long_sets_2500_tokens(self):
        messages = [{"role": "user", "content": "Hi"}]
        captured = []

        async def capture_api(payload):
            captured.append(payload)
            return _make_text_reply("ok")

        with patch("agent.brain._call_api", new=capture_api):
            await think(messages, length_mode="long")

        assert captured[0]["max_tokens"] == 2500

    @pytest.mark.asyncio
    async def test_invalid_length_mode_defaults_to_600_tokens(self):
        messages = [{"role": "user", "content": "Hi"}]
        captured = []

        async def capture_api(payload):
            captured.append(payload)
            return _make_text_reply("ok")

        with patch("agent.brain._call_api", new=capture_api):
            await think(messages, length_mode="ultralong")

        assert captured[0]["max_tokens"] == 600

    @pytest.mark.asyncio
    async def test_tool_log_records_tool_name_and_args(self):
        messages = [{"role": "user", "content": "news?"}]
        first_response = _make_tool_call_reply("search_news", {"query": "台灣"})
        second_response = _make_text_reply("Here's the news.")

        with patch("agent.brain._call_api", new=AsyncMock(side_effect=[first_response, second_response])), \
             patch("agent.brain._execute_tool", new=AsyncMock(return_value="Top story")):
            _, tool_log = await think(messages)

        assert tool_log[0]["tool"] == "search_news"
        assert tool_log[0]["args"] == {"query": "台灣"}
