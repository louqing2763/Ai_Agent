"""
agent/brain.py — 意圖理解 + 工具選擇

v3.0 新增：
  - think_agentic(): Agentic thinking loop
    Step 1 — OpenAI (gpt-4o-mini) 規劃（要不要用工具、要展開哪個話題）
    Step 2 — 執行工具（只跑需要的）
    Step 3 — DeepSeek 生成最終回覆（帶著規劃結果）

v2.1：
  - think_stream(): 串流版推理
  - 依賴 httpx 支援 async streaming
"""

import os
import json
import asyncio
import logging
from typing import Optional, AsyncGenerator

import httpx

VERSION_BRAIN = "3.0"
logger = logging.getLogger(__name__)

# ── DeepSeek（主要生成）──────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL   = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# ── OpenAI（規劃用）─────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_URL        = "https://api.openai.com/v1/chat/completions"
OPENAI_PLAN_MODEL = os.getenv("OPENAI_PLAN_MODEL", "gpt-4o-mini")

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

# 工具名稱列表（供規劃步驟參考）
TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS]


# ----------------------------------------------------------
# 🤖 Agentic Thinking Loop（v3.0 核心）
# ----------------------------------------------------------
async def think_agentic(
    messages: list,
    length_mode: str = "normal",
    tools_enabled: bool = True,
) -> tuple[str, list, dict]:
    """
    三步驟 Agentic loop：

    Step 1 — OpenAI (gpt-4o-mini) 規劃
    Step 2 — 執行工具（只跑規劃指定的）
    Step 3 — DeepSeek 生成最終回覆

    Returns:
      (reply, tool_log, plan)
    """
    tool_log = []
    plan     = {}

    # ── Step 1：OpenAI 規劃 ───────────────────────────────
    plan = await _openai_plan(messages, tools_enabled)
    logger.info(f"[agentic] 規劃結果: {plan}")

    # ── Step 2：執行工具 ──────────────────────────────────
    tool_context = ""
    if tools_enabled and plan.get("need_tools"):
        for tool_name in plan["need_tools"]:
            if tool_name not in TOOL_NAMES:
                continue
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"),
                ""
            )
            args   = _infer_tool_args(tool_name, last_user)
            result = await _execute_tool(tool_name, args)
            tool_log.append({"tool": tool_name, "args": args, "result": result})
            tool_context += f"\n[工具結果 - {tool_name}]\n{result}\n"

    # ── Step 3：DeepSeek 生成 ─────────────────────────────
    plan_injection = _build_plan_injection(plan, tool_context)
    messages_with_plan = messages.copy()

    if plan_injection:
        messages_with_plan = messages[:-1] + [
            {"role": "system", "content": plan_injection},
            messages[-1],
        ] if messages and messages[-1]["role"] == "user" else messages + [
            {"role": "system", "content": plan_injection}
        ]

    max_tokens_map = {"short": 150, "normal": 600, "long": 2500, "auto": 1200}
    if length_mode == "auto" and plan.get("response_length"):
        max_tokens = max_tokens_map.get(plan["response_length"], 600)
    else:
        max_tokens = max_tokens_map.get(length_mode, 600)
      
    payload = {
        "model":             DEEPSEEK_MODEL,
        "messages":          messages_with_plan,
        "temperature":       1.4,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
    }

    response = await _call_deepseek_api(payload)
    if response is None:
        return "(連線中斷，請稍後再試)", tool_log, plan

    reply = response["choices"][0]["message"].get("content", "")
    return reply, tool_log, plan


def _build_plan_injection(plan: dict, tool_context: str) -> str:
    """把規劃結果轉成注入 system 的提示"""
    parts = []

    if tool_context:
        parts.append(tool_context)

    topic = plan.get("topic_to_expand", "")
    if topic:
        parts.append(
            f"（OOC·規劃）這次對話裡有一個值得深入的點：{topic}\n"
            f"如果自然的話，可以往這個方向展開，不要強迫，但不要錯過。"
        )

    approach = plan.get("approach", "")
    if approach:
        parts.append(f"（OOC·方向）{approach}")

    return "\n".join(parts)


def _infer_tool_args(tool_name: str, user_text: str) -> dict:
    """從 user 訊息簡單推斷工具參數"""
    if tool_name == "get_weather":
        import re
        common_cities = [
            "台北", "新北", "桃園", "台中", "台南", "高雄", "新竹", "基隆",
            "嘉義", "屏東", "宜蘭", "花蓮", "台東", "苗栗", "彰化", "南投",
            "雲林", "澎湖", "金門", "馬祖",
            "東京", "大阪", "京都", "首爾", "新加坡", "曼谷", "吉隆坡",
            "香港", "上海", "北京", "深圳", "紐約", "倫敦", "巴黎",
            "Tokyo", "Osaka", "Seoul", "Singapore", "Bangkok",
            "Hong Kong", "Shanghai", "Beijing", "New York", "London", "Paris",
        ]
        for city in common_cities:
            if city.lower() in user_text.lower():
                return {"city": city}
        weather_match = re.search(r'([\w]+)(?:的?天氣|氣溫|溫度|weather)', user_text)
        if weather_match:
            return {"city": weather_match.group(1)}
        return {"city": "台北"}
    elif tool_name == "search_news":
        return {"query": user_text[:30]}
    elif tool_name == "get_system_status":
        return {"detail": "all"}
    return {}


# ----------------------------------------------------------
# 🌐 OpenAI 規劃（gpt-4o-mini）
# ----------------------------------------------------------
async def _openai_plan(messages: list, tools_enabled: bool) -> dict:
    """
    用 OpenAI 輕量模型做規劃。
    只看最近 6 條對話，輸出 JSON。
    失敗時回傳空規劃（fallback 到標準 think）。
    """
    if not OPENAI_API_KEY:
        return {}

    recent = messages[-6:] if len(messages) > 6 else messages
    convo  = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Lilith' if m['role'] == 'assistant' else 'System'}: "
        f"{m['content'][:200]}"
        for m in recent
        if m["role"] in ("user", "assistant")
    )

    tools_hint = (
        f"可用工具：{', '.join(TOOL_NAMES)}" if tools_enabled
        else "這次不使用工具。"
    )

    prompt = f"""你是一個對話規劃助手。根據以下對話片段，決定下一步的回覆策略。

對話：
{convo}

{tools_hint}

請只回傳 JSON，不要有任何其他文字：
{{
  "need_tools": [],
  "topic_to_expand": "",
  "approach": "",
  "response_length": "normal"
}}

說明：
- need_tools: 這次回覆需要呼叫的工具名稱列表（空列表代表不需要）
- topic_to_expand: 對話裡有沒有值得深入探討的話題或情緒？用一句話描述，沒有就留空
- approach: 這次回覆的話題方向，用一句話描述。只管「聊什麼」，不管「怎麼聊」。不要給情緒處理建議——情緒回應由回覆者自己決定。沒有特別的就留空
- response_length: 回覆長度。"short"=簡短回應（打招呼、是/否、閒聊附和）、"normal"=一般對話、"long"=需要詳細解釋或深入討論。根據 User 的訊息複雜度決定，不是根據話題重要性。
"""

    try:
        payload = {
            "model":       OPENAI_PLAN_MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens":  200,
        }
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(OPENAI_URL, headers=headers, json=payload)

        if res.status_code != 200:
            logger.warning(f"[plan] 規劃失敗: {res.status_code}")
            return {}

        text = res.json()["choices"][0]["message"]["content"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        plan = json.loads(text)

        plan.setdefault("need_tools", [])
        plan.setdefault("topic_to_expand", "")
        plan.setdefault("approach", "")
        plan["need_tools"] = [t for t in plan["need_tools"] if t in TOOL_NAMES]

        return plan

    except Exception as e:
        logger.warning(f"[plan] 規劃例外: {e}")
        return {}


# ----------------------------------------------------------
# 🧠 標準推理（非串流，向下相容）
# ----------------------------------------------------------
async def think(
    messages: list,
    length_mode: str = "normal",
    tools_enabled: bool = True,
) -> tuple[str, list]:
    max_tokens_map = {"short": 150, "normal": 600, "long": 2500, "auto": 1200}
    max_tokens     = max_tokens_map.get(length_mode, 600)
    tool_calls_log = []

    payload = {
        "model":             DEEPSEEK_MODEL,
        "messages":          messages,
        "temperature":       1.4,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
    }
    if tools_enabled:
        payload["tools"]       = TOOL_DEFINITIONS
        payload["tool_choice"] = "auto"

    response = await _call_deepseek_api(payload)
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
        "model":             DEEPSEEK_MODEL,
        "messages":          messages_with_results,
        "temperature":       1.25,
        "max_tokens":        max_tokens,
        "presence_penalty":  0.6,
        "frequency_penalty": 0.2,
    }
    final_response = await _call_deepseek_api(final_payload)
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
    max_tokens_map = {"short": 150, "normal": 600, "long": 2500, "auto": 1200}
    max_tokens     = max_tokens_map.get(length_mode, 600)

    payload = {
        "model":             DEEPSEEK_MODEL,
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
            "model":             DEEPSEEK_MODEL,
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
# 🌐 DeepSeek API 呼叫（非串流，含重試）
# ----------------------------------------------------------
_MAX_RETRIES = 3

async def _call_deepseek_api(payload: dict) -> Optional[dict]:
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(DEEPSEEK_URL, headers=headers, json=payload)
            if res.status_code == 200:
                return res.json()
            logger.error(f"[brain] DeepSeek API 錯誤: {res.status_code} {res.text[:200]}")
            if res.status_code < 500:
                return None
        except Exception as e:
            logger.error(f"[brain] DeepSeek API 呼叫失敗 (attempt {attempt+1}): {e}")
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(2 ** attempt)
    return None
