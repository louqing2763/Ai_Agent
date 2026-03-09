"""
tools/datetime_tool.py
"""
from datetime import datetime
import pytz

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
WEEKDAY_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

def get_current_time() -> dict:
      now = datetime.now(TAIPEI_TZ)
      return {
          "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
          "date":     now.strftime("%Y年%m月%d日"),
          "time":     now.strftime("%H:%M"),
          "weekday":  WEEKDAY_ZH[now.weekday()],
          "hour":     now.hour,
          "period":   _get_period(now.hour),
      }

def _get_period(hour: int) -> str:
      if 0 <= hour < 6:   return "深夜"
            if 6 <= hour < 12:  return "早上"
                  if 12 <= hour < 18: return "下午"
                        if 18 <= hour < 22: return "晚上"
                              return "深夜"
