"""
datetime_tool.py — 時間與日期查詢工具

提供莉莉絲準確的當前時間、日期、星期資訊。
DeepSeek 本身沒有即時時鐘，這個工具讓她可以在被問到時自己查詢。
"""

from datetime import datetime
import pytz

# 台灣時區
TW_TZ = pytz.timezone("Asia/Taipei")

WEEKDAY_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def get_current_datetime() -> dict:
    """
    回傳當前台灣時間的完整資訊。

    Returns:
        dict: 包含時間各個面向的資訊
    """
    now = datetime.now(TW_TZ)
    weekday_zh = WEEKDAY_ZH[now.weekday()]

    return {
        "datetime_str": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "time_full": now.strftime("%H:%M:%S"),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "weekday": weekday_zh,
        "weekday_en": now.strftime("%A"),
        "timestamp": int(now.timestamp()),
        # 自然語言描述，直接可以注入給 LLM 使用
        "natural": f"{now.year}年{now.month}月{now.day}日，{weekday_zh}，{now.strftime('%H:%M')}",
    }


def get_time_period(hour: int = None) -> str:
    """
    根據小時判斷時段描述。
    hour 為 None 時自動取當前時間。
    """
    if hour is None:
        hour = datetime.now(TW_TZ).hour

    if 0 <= hour < 6:
        return "深夜"
    elif 6 <= hour < 9:
        return "早晨"
    elif 9 <= hour < 12:
        return "上午"
    elif 12 <= hour < 14:
        return "中午"
    elif 14 <= hour < 18:
        return "下午"
    elif 18 <= hour < 22:
        return "晚上"
    else:
        return "深夜"


# ── DeepSeek Function Calling 格式的工具定義 ──────────────────────────────

DATETIME_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": (
            "查詢當前的台灣時間、日期和星期。"
            "當使用者問『現在幾點』、『今天幾號』、『今天星期幾』、"
            "『現在是什麼時候』等問題時，呼叫此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def handle_datetime_tool_call() -> str:
    """
    處理 LLM 發出的 get_current_datetime 工具呼叫。
    回傳格式化的時間字串供 LLM 使用。
    """
    info = get_current_datetime()
    return (
        f"現在時間：{info['natural']}\n"
        f"完整時間：{info['datetime_str']}（台灣時區 UTC+8）"
    )
