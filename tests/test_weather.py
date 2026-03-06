"""
tests/test_weather.py — Unit tests for tools/weather.py

Coverage areas:
- WMO_CODES mapping: known codes, unknown codes
- _geocode(): success and empty response
- _fetch_weather(): parses "current" block from API response
- get_weather(): full happy path, geocoding failure, weather API failure,
                 exception handling
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.weather import get_weather, _geocode, _fetch_weather, WMO_CODES


# ------------------------------------------------------------------
# WMO_CODES mapping
# ------------------------------------------------------------------
class TestWmoCodes:
    def test_known_code_0_is_sunny(self):
        assert WMO_CODES[0] == "晴天"

    def test_known_code_63_is_medium_rain(self):
        assert WMO_CODES[63] == "中雨"

    def test_known_code_95_is_thunderstorm(self):
        assert WMO_CODES[95] == "雷陣雨"

    def test_known_code_75_is_heavy_snow(self):
        assert WMO_CODES[75] == "大雪"

    def test_unknown_code_returns_fallback(self):
        # The code 999 is not in WMO_CODES; .get() should return the default
        assert WMO_CODES.get(999, "不明") == "不明"

    def test_all_codes_are_non_empty_strings(self):
        for code, desc in WMO_CODES.items():
            assert isinstance(desc, str) and len(desc) > 0, f"WMO code {code} has empty description"


# ------------------------------------------------------------------
# Helper: build a mock aiohttp response
# ------------------------------------------------------------------
def make_mock_response(json_data):
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def make_mock_session(response):
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=response)
    return mock_session


# ------------------------------------------------------------------
# _geocode
# ------------------------------------------------------------------
class TestGeocode:
    @pytest.mark.asyncio
    async def test_returns_lat_lon_and_display_name(self):
        geo_data = [{"lat": "25.0330", "lon": "121.5654", "display_name": "台北市"}]
        mock_resp = make_mock_response(geo_data)
        mock_session = make_mock_session(mock_resp)

        with patch("tools.weather.aiohttp.ClientSession", return_value=mock_session):
            lat, lon, name = await _geocode("台北")

        assert abs(lat - 25.0330) < 0.001
        assert abs(lon - 121.5654) < 0.001
        assert name == "台北市"

    @pytest.mark.asyncio
    async def test_returns_none_tuple_when_not_found(self):
        mock_resp = make_mock_response([])
        mock_session = make_mock_session(mock_resp)

        with patch("tools.weather.aiohttp.ClientSession", return_value=mock_session):
            lat, lon, name = await _geocode("NonexistentCity123")

        assert lat is None
        assert lon is None
        assert name is None

    @pytest.mark.asyncio
    async def test_uses_display_name_fallback_to_city(self):
        # Item without 'display_name' key
        geo_data = [{"lat": "35.6762", "lon": "139.6503"}]
        mock_resp = make_mock_response(geo_data)
        mock_session = make_mock_session(mock_resp)

        with patch("tools.weather.aiohttp.ClientSession", return_value=mock_session):
            lat, lon, name = await _geocode("Tokyo")

        assert name == "Tokyo"  # Falls back to the city argument


# ------------------------------------------------------------------
# _fetch_weather
# ------------------------------------------------------------------
class TestFetchWeather:
    @pytest.mark.asyncio
    async def test_returns_current_weather_block(self):
        weather_data = {
            "current": {
                "temperature_2m": 25.5,
                "apparent_temperature": 27.0,
                "relative_humidity_2m": 70,
                "weather_code": 1,
                "wind_speed_10m": 12.3,
            }
        }
        mock_resp = make_mock_response(weather_data)
        mock_session = make_mock_session(mock_resp)

        with patch("tools.weather.aiohttp.ClientSession", return_value=mock_session):
            result = await _fetch_weather(25.03, 121.56)

        assert result["temperature_2m"] == 25.5
        assert result["weather_code"] == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_no_current_block(self):
        mock_resp = make_mock_response({})
        mock_session = make_mock_session(mock_resp)

        with patch("tools.weather.aiohttp.ClientSession", return_value=mock_session):
            result = await _fetch_weather(0.0, 0.0)

        assert result is None


# ------------------------------------------------------------------
# get_weather — full pipeline
# ------------------------------------------------------------------
class TestGetWeather:
    @pytest.mark.asyncio
    async def test_happy_path_returns_formatted_string(self):
        geo_result = (25.03, 121.56, "台北市, 台灣")
        weather_result = {
            "temperature_2m": 28.0,
            "apparent_temperature": 30.0,
            "relative_humidity_2m": 80,
            "weather_code": 0,
            "wind_speed_10m": 8.5,
        }
        with patch("tools.weather._geocode", new=AsyncMock(return_value=geo_result)), \
             patch("tools.weather._fetch_weather", new=AsyncMock(return_value=weather_result)):
            result = await get_weather("台北")

        assert "台北市" in result
        assert "晴天" in result
        assert "28.0" in result
        assert "30.0" in result
        assert "80%" in result
        assert "8.5" in result

    @pytest.mark.asyncio
    async def test_geocoding_failure_returns_not_found_message(self):
        with patch("tools.weather._geocode", new=AsyncMock(return_value=(None, None, None))):
            result = await get_weather("UnknownCity")

        assert "找不到" in result
        assert "UnknownCity" in result

    @pytest.mark.asyncio
    async def test_weather_api_failure_returns_error_message(self):
        with patch("tools.weather._geocode", new=AsyncMock(return_value=(25.0, 121.0, "台北"))), \
             patch("tools.weather._fetch_weather", new=AsyncMock(return_value=None)):
            result = await get_weather("台北")

        assert "暫時無法取得" in result

    @pytest.mark.asyncio
    async def test_exception_returns_empty_string(self):
        with patch("tools.weather._geocode", new=AsyncMock(side_effect=Exception("network error"))):
            result = await get_weather("台北")

        assert result == ""

    @pytest.mark.asyncio
    async def test_unknown_wmo_code_shows_unknown_description(self):
        geo_result = (25.03, 121.56, "台北")
        weather_result = {
            "temperature_2m": 20.0,
            "apparent_temperature": 19.0,
            "relative_humidity_2m": 60,
            "weather_code": 9999,  # Unknown WMO code
            "wind_speed_10m": 5.0,
        }
        with patch("tools.weather._geocode", new=AsyncMock(return_value=geo_result)), \
             patch("tools.weather._fetch_weather", new=AsyncMock(return_value=weather_result)):
            result = await get_weather("台北")

        assert "不明" in result

    @pytest.mark.asyncio
    async def test_default_city_is_taipei(self):
        geo_result = (25.03, 121.56, "台北市")
        weather_result = {
            "temperature_2m": 22.0,
            "apparent_temperature": 21.0,
            "relative_humidity_2m": 70,
            "weather_code": 2,
            "wind_speed_10m": 10.0,
        }
        with patch("tools.weather._geocode", new=AsyncMock(return_value=geo_result)) as mock_geo, \
             patch("tools.weather._fetch_weather", new=AsyncMock(return_value=weather_result)):
            await get_weather()  # No city argument

        mock_geo.assert_called_once_with("台北")
