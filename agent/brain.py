"""
agent/brain.py — 意圖理解 + 工具選擇

v2.1 新增：
  - think_stream(): 串流版推理，供通話模式使用
  - 依賴 httpx（非 requests）以支援 async streaming
"""

import os
import json
import asyncio
import logging
from typing import Optional, AsyncGenerator

import httpx
import requests

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"

# ----------------------------------------------------------
# 🔧 工具定義
# ----------------------------------------------------------
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": (
                "查詢當前的台灣時間、日期和星期。"
                "當 User 問『現在幾點』、『今天幾號』、『今天星期幾』等問題時使用。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "搜尋最新新聞或網路資訊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜尋關鍵字。"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "查詢電腦系統狀態。",
            "parameters": {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string",
                        "enum": ["all", "cpu", "memory", "disk", "network", "processes"],
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查詢天氣資訊。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名稱。"}
                },
                "required": ["city"],
            },
        },
    },
]


# ----------------------------------------------------------
# 🧠 標準推理（非串流）
# ----------------------------------------------------------
async def think(
    messages: list,
    length_mode: str = "normal",
    tools_enabled: bool = True,
) -> tuple[str, list]:
    max_tokens_map = {"short": 150, "normal": 600, "long": 2500}
    max_tokens     = max_tokens_map.get(length_mode, 600)
    tool_calls_log = []

    payload = {
        "model":             "deepseek-chat",
        "messages":          messages,
        "temperature":       1.4,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
    }
    if tools_enabled:
        payload["tools"]       = TOOL_DEFINITIONS
        payload["tool_choice"] = "auto"

    response = await _call_api(payload)
    if response is None:
        return "(連線中斷，請稍後再試)", []

    choice  = response["choices"][0]
    message = choice["message"]

    if not message.get("tool_calls"):
        return message.get("content", ""), tool_calls_log

    tool_results = []
    for tc in message["tool_calls"]:
        fn_name = tc["function"]["name"]
        fn_args = json.loads(tc["function"]["arguments"] or "{}")
        tc_id   = tc["id"]
        result  = await _execute_tool(fn_name, fn_args)
        tool_calls_log.append({"tool": fn_name, "args": fn_args, "result": result})
        tool_results.append({"role": "tool", "tool_call_id": tc_id, "content": result})

    if not tool_results:
        return message.get("content", ""), tool_calls_log

    messages_with_results = messages + [
        {"role": "assistant", "tool_calls": message["tool_calls"]},
        *tool_results,
    ]
    final_payload = {
        "model":             "deepseek-chat",
        "messages":          messages_with_results,
        "temperature":       1.25,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
    }
    final_response = await _call_api(final_payload)
    if final_response is None:
        return "(工具執行完畢，但生成回覆時發生錯誤)", tool_calls_log

    return final_response["choices"][0]["message"].get("content", ""), tool_calls_log


# ----------------------------------------------------------
# 🌊 串流推理（通話模式用）
# ----------------------------------------------------------
async def think_stream(
    messages: list,
    length_mode: str = "normal",
) -> AsyncGenerator[str, None]:
    """
    串流版推理：以 async generator 逐段 yield 文字。
    工具呼叫仍同步處理完畢，再串流最終回覆。
    """
    max_tokens_map = {"short": 150, "normal": 600, "long": 2500}
    max_tokens     = max_tokens_map.get(length_mode, 600)

    payload = {
        "model":             "deepseek-chat",
        "messages":          messages,
        "temperature":       1.4,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
        "stream":            True,
        "tools":             TOOL_DEFINITIONS,
        "tool_choice":       "auto",
    }
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }

    tool_calls_acc = {}

    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream("POST", DEEPSEEK_URL, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                yield "(連線失敗)"
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue

                delta = chunk["choices"][0].get("delta", {})

                if delta.get("content"):
                    yield delta["content"]

                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc.get("id", ""), "name": "", "arguments": ""
                        }
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        tool_calls_acc[idx]["name"] = fn["name"]
                    if fn.get("arguments"):
                        tool_calls_acc[idx]["arguments"] += fn["arguments"]

    # 有工具呼叫：執行後再串流最終回覆
    if tool_calls_acc:
        tool_results = []
        for tc in tool_calls_acc.values():
            try:
                args   = json.loads(tc["arguments"] or "{}")
                result = await _execute_tool(tc["name"], args)
            except Exception as e:
                result = f"工具執行失敗: {e}"
            tool_results.append({
                "role": "tool", "tool_call_id": tc["id"], "content": result
            })

        fake_tool_calls = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for tc in tool_calls_acc.values()
        ]
        messages2 = messages + [
            {"role": "assistant", "tool_calls": fake_tool_calls},
            *tool_results,
        ]
        payload2 = {
            "model":             "deepseek-chat",
            "messages":          messages2,
            "temperature":       1.25,
            "max_tokens":        max_tokens,
            "presence_penalty":  0.6,
            "frequency_penalty": 0.2,
            "stream":            True,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream(
                "POST", DEEPSEEK_URL, headers=headers, json=payload2
            ) as resp2:
                async for line in resp2.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    content = chunk["choices"][0].get("delta", {}).get("content", "")
                    if content:
                        yield content


# ----------------------------------------------------------
# 🔧 工具執行
# ----------------------------------------------------------
async def _execute_tool(fn_name: str, fn_args: dict) -> str:
    try:
        if fn_name == "get_current_datetime":
            from tools.datetime_tool import handle_datetime_tool_call
            return handle_datetime_tool_call()
        elif fn_name == "search_news":
            from core.news import search_news
            result = await search_news(fn_args.get("query", ""))
            return result or "沒有找到相關新聞。"
        elif fn_name == "get_system_status":
            from tools.system_monitor import get_system_status
            result = await get_system_status(fn_args.get("detail", "all"))
            return result or "系統狀態暫時無法取得。"
        elif fn_name == "get_weather":
            from tools.weather import get_weather
            result = await get_weather(fn_args.get("city", "台北"))
            return result or "天氣資訊暫時無法取得。"
        else:
            return f"未知工具：{fn_name}"
    except Exception as e:
        logger.error(f"[brain] 工具執行失敗 {fn_name}: {e}")
        return f"工具執行時發生錯誤：{e}"


# ----------------------------------------------------------
# 🌐 API 呼叫（非串流）
# ----------------------------------------------------------
async def _call_api(payload: dict) -> Optional[dict]:
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    try:
        res = await asyncio.to_thread(
            requests.post, DEEPSEEK_URL,
            headers=headers, json=payload, timeout=60,
        )
        if res.status_code == 200:
            return res.json()
        logger.error(f"[brain] API 錯誤: {res.status_code} {res.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"[brain] API 呼叫失敗: {e}")
        return None
