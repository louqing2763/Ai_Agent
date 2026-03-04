"""
tools/weather.py — 天氣查詢工具

使用 Open-Meteo API（完全免費，無需 API Key）
+ Nominatim geocoding（城市名 → 座標）

安裝：pip install aiohttp（已在 requirements.txt）
"""

import logging
import aiohttp

logger = logging.getLogger(__name__)

# Open-Meteo WMO 天氣代碼對應中文描述
WMO_CODES = {
    0:  "晴天",
    1:  "大致晴朗", 2: "局部多雲", 3: "陰天",
    45: "有霧", 48: "霧凇",
    51: "毛毛雨（輕）", 53: "毛毛雨（中）", 55: "毛毛雨（強）",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒",
    80: "陣雨（輕）", 81: "陣雨（中）", 82: "陣雨（強）",
    85: "陣雪（輕）", 86: "陣雪（強）",
    95: "雷陣雨",
    96: "雷陣雨夾冰雹（輕）", 99: "雷陣雨夾冰雹（強）",
}


async def get_weather(city: str = "台北") -> str:
    """
    查詢指定城市的當前天氣。

    Args:
        city: 城市名稱（中英文皆可）

    Returns:
        格式化的天氣描述字串；失敗時回傳空字串
    """
    try:
        # Step 1: 城市名 → 座標（Nominatim geocoding）
        lat, lon, display_name = await _geocode(city)
        if lat is None:
            return f"找不到「{city}」的位置資訊。"

        # Step 2: 座標 → 天氣（Open-Meteo）
        weather = await _fetch_weather(lat, lon)
        if weather is None:
            return "天氣資訊暫時無法取得。"

        # Step 3: 格式化
        temp     = weather.get("temperature_2m", "?")
        feels    = weather.get("apparent_temperature", "?")
        humidity = weather.get("relative_humidity_2m", "?")
        wmo      = weather.get("weather_code", 0)
        wind     = weather.get("wind_speed_10m", "?")
        desc     = WMO_CODES.get(wmo, "不明")

        return (
            f"📍 {display_name}\n"
            f"🌤 {desc}\n"
            f"🌡 氣溫 {temp}°C（體感 {feels}°C）\n"
            f"💧 濕度 {humidity}%\n"
            f"💨 風速 {wind} km/h"
        )

    except Exception as e:
        logger.error(f"[weather] 查詢失敗: {e}")
        return ""


async def _geocode(city: str) -> tuple:
    """城市名 → (lat, lon, display_name)"""
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q":              city,
        "format":         "json",
        "limit":          1,
        "accept-language": "zh-TW",
    }
    headers = {"User-Agent": "LilithAgent/2.0"}

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            data = await resp.json()

    if not data:
        return None, None, None

    item = data[0]
    return float(item["lat"]), float(item["lon"]), item.get("display_name", city)


async def _fetch_weather(lat: float, lon: float) -> dict:
    """座標 → 當前天氣數據"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude":       lat,
        "longitude":      lon,
        "current":        ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "weather_code",
            "wind_speed_10m",
        ]),
        "timezone":       "Asia/Taipei",
        "forecast_days":  1,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            data = await resp.json()

    return data.get("current")
