"""
agent/brain.py — 意圖理解 + 工具選擇

取代原本 main.py 裡的 if/else 字串比對。
使用 DeepSeek function calling 讓 LLM 自己決定要不要呼叫工具。

流程：
  1. 把使用者訊息 + 可用工具定義送給 DeepSeek
  2. DeepSeek 回傳：純回覆 OR 工具呼叫指令
  3. 若是工具呼叫 → 執行工具 → 把結果塞回 context → 再次呼叫 LLM 生成最終回覆
  4. 若是純回覆 → 直接回傳
"""

import os
import json
import asyncio
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"

# ----------------------------------------------------------
# 🔧 工具定義（告訴 LLM 有哪些工具可用）
# ----------------------------------------------------------
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "搜尋最新新聞或網路資訊。"
                "當 User 問到時事、新聞、最新消息、或需要查詢特定事件時使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜尋關鍵字，例如「台灣科技新聞」。空字串代表取最新頭條。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": (
                "查詢電腦系統狀態。"
                "當 User 問電腦狀況、CPU、記憶體、磁碟、網路、或目前開著什麼程式時使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "detail": {
                        "type": "string",
                        "description": "查詢項目：all全部、cpu、memory、disk、network、processes目前程式",
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
            "description": (
                "查詢天氣資訊。"
                "當 User 問天氣、溫度、下雨機率、今天穿什麼時使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名稱，例如「台北」、「Tokyo」。",
                    }
                },
                "required": ["city"],
            },
        },
    },
]


# ----------------------------------------------------------
# 🧠 主要入口
# ----------------------------------------------------------
async def think(
    messages: list,
    length_mode: str = "normal",
    tools_enabled: bool = True,
) -> tuple[str, list]:
    """
    核心推理函式。

    Args:
        messages:      完整的對話歷史（含 system prompt）
        length_mode:   "short" / "normal" / "long"
        tools_enabled: 是否啟用工具呼叫

    Returns:
        (final_reply: str, tool_calls_log: list)
        tool_calls_log 記錄這次用了哪些工具及其結果（供日誌與快取用）
    """
    max_tokens_map = {"short": 150, "normal": 600, "long": 2500}
    max_tokens     = max_tokens_map.get(length_mode, 600)

    tool_calls_log = []

    # 第一次呼叫：判斷要不要用工具
    payload = {
        "model":             "deepseek-chat",
        "messages":          messages,
        "temperature":       1.25,
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

    # ------ 純文字回覆：直接回傳 ------
    if not message.get("tool_calls"):
        return message.get("content", ""), tool_calls_log

    # ------ 工具呼叫 ------
    tool_results = []

    for tc in message["tool_calls"]:
        fn_name = tc["function"]["name"]
        fn_args = json.loads(tc["function"]["arguments"] or "{}")
        tc_id   = tc["id"]

        logger.info(f"[brain] 工具呼叫: {fn_name}({fn_args})")

        # 執行工具
        result = await _execute_tool(fn_name, fn_args)

        tool_calls_log.append({"tool": fn_name, "args": fn_args, "result": result})

        tool_results.append({
            "role":         "tool",
            "tool_call_id": tc_id,
            "content":      result,
        })

    if not tool_results:
        return message.get("content", ""), tool_calls_log

    # 把工具結果塞回 context，再讓 LLM 生成最終回覆
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
        # 拿到工具結果後不再呼叫工具，避免無限迴圈
    }

    final_response = await _call_api(final_payload)
    if final_response is None:
        return "(工具執行完畢，但生成回覆時發生錯誤)", tool_calls_log

    final_reply = final_response["choices"][0]["message"].get("content", "")
    return final_reply, tool_calls_log


# ----------------------------------------------------------
# 🔧 工具執行
# ----------------------------------------------------------
async def _execute_tool(fn_name: str, fn_args: dict) -> str:
    """派發工具呼叫到對應模組"""
    try:
        if fn_name == "search_news":
            from core.news import search_news
            query  = fn_args.get("query", "")
            result = await search_news(query)
            return result or "沒有找到相關新聞。"

        elif fn_name == "get_system_status":
            from tools.system_monitor import get_system_status
            detail = fn_args.get("detail", "all")
            result = await get_system_status(detail)
            return result or "系統狀態暫時無法取得。"

        elif fn_name == "get_weather":
            from tools.weather import get_weather
            city   = fn_args.get("city", "台北")
            result = await get_weather(city)
            return result or "天氣資訊暫時無法取得。"

        else:
            return f"未知工具：{fn_name}"

    except Exception as e:
        logger.error(f"[brain] 工具執行失敗 {fn_name}: {e}")
        return f"工具執行時發生錯誤：{e}"


# ----------------------------------------------------------
# 🌐 API 呼叫
# ----------------------------------------------------------
async def _call_api(payload: dict) -> Optional[dict]:
    """呼叫 DeepSeek API，失敗時回傳 None"""
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    try:
        res = await asyncio.to_thread(
            requests.post,
            DEEPSEEK_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )
        if res.status_code == 200:
            return res.json()
        logger.error(f"[brain] API 錯誤: {res.status_code} {res.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"[brain] API 呼叫失敗: {e}")
        return None
