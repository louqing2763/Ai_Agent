"""
tests/test_persona_config.py — Unit tests for core/persona_config.py

Coverage areas:
- get_persona(): presence of base identity in output
- Language style modes (short / normal / long)
- Time-of-day rules applied based on hour
- Absence rules applied based on minutes_since_last / timer_trigger
- News injection when news string is non-empty
- Redis full-template override via _load_full_template
"""

import json
import pytest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.persona_config import (
    get_persona,
    _load_full_template,
    BASE_IDENTITY,
    STYLE_SHORT,
    STYLE_NORMAL,
    STYLE_LONG,
    TIME_RULES,
    ABSENCE_RULES,
    NEWS_RULES,
)


# ------------------------------------------------------------------
# _load_full_template
# ------------------------------------------------------------------
class TestLoadFullTemplate:
    def test_returns_empty_dict_when_no_redis(self):
        result = _load_full_template(None)
        assert result == {}

    def test_returns_empty_dict_when_redis_key_missing(self):
        redis = MagicMock()
        redis.get.return_value = None
        result = _load_full_template(redis)
        assert result == {}

    def test_returns_parsed_json_from_redis(self):
        redis = MagicMock()
        template = {"base_identity": "Custom identity"}
        redis.get.return_value = json.dumps(template)
        result = _load_full_template(redis)
        assert result == template

    def test_returns_empty_dict_on_redis_exception(self):
        redis = MagicMock()
        redis.get.side_effect = Exception("connection error")
        result = _load_full_template(redis)
        assert result == {}


# ------------------------------------------------------------------
# get_persona — base output
# ------------------------------------------------------------------
class TestGetPersonaBase:
    def test_contains_base_identity(self):
        result = get_persona()
        assert "莉莉絲" in result or "Lilith" in result

    def test_contains_system_clock_line(self):
        result = get_persona()
        assert "[系統時鐘]" in result

    def test_contains_output_check(self):
        result = get_persona()
        assert "說話前" in result or "Lilith" in result


# ------------------------------------------------------------------
# get_persona — language style modes
# ------------------------------------------------------------------
class TestGetPersonaStyleModes:
    def test_short_mode_includes_style_short(self):
        result = get_persona(length_mode="short")
        assert "省流" in result

    def test_normal_mode_includes_style_normal(self):
        result = get_persona(length_mode="normal")
        assert "標準" in result

    def test_long_mode_includes_style_long(self):
        result = get_persona(length_mode="long")
        assert "深度" in result

    def test_invalid_mode_falls_back_to_normal(self):
        # Invalid mode should fall back to STYLE_NORMAL
        result = get_persona(length_mode="invalid_mode")
        assert "標準" in result


# ------------------------------------------------------------------
# get_persona — time-of-day rules
# ------------------------------------------------------------------
class TestGetPersonaTimeRules:
    def _get_with_hour(self, hour: int, **kwargs) -> str:
        fake_now = MagicMock()
        fake_now.hour = hour
        fake_now.strftime.return_value = f"{hour:02d}:00"
        with patch("core.persona_config.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            return get_persona(**kwargs)

    def test_deep_night_rule_at_3am(self):
        result = self._get_with_hour(3)
        assert "凌晨" in result

    def test_early_morning_rule_at_6am(self):
        result = self._get_with_hour(6)
        assert "清晨" in result

    def test_morning_rule_at_8am(self):
        result = self._get_with_hour(8)
        assert "早晨" in result

    def test_noon_rule_at_13(self):
        result = self._get_with_hour(13)
        assert "午間" in result

    def test_night_rule_at_22(self):
        result = self._get_with_hour(22)
        assert "夜晚" in result

    def test_no_special_rule_at_noon(self):
        # 11:00 is not covered by any special rule
        result = self._get_with_hour(11)
        # None of the special time blocks should appear
        assert "凌晨" not in result
        assert "清晨" not in result
        assert "早晨" not in result
        assert "午間" not in result
        assert "夜晚" not in result


# ------------------------------------------------------------------
# get_persona — absence rules
# ------------------------------------------------------------------
class TestGetPersonaAbsenceRules:
    def test_timer_trigger_adds_timer_rule(self):
        result = get_persona(timer_trigger=True)
        assert "主動聯絡" in result

    def test_long_absence_over_480_minutes(self):
        result = get_persona(minutes_since_last=600)
        assert "一整天" in result

    def test_medium_absence_between_120_and_480(self):
        result = get_persona(minutes_since_last=200)
        assert "短_absence" in result or "離開一段時間" in result

    def test_no_absence_rule_when_recent(self):
        # When minutes_since_last is small and no timer_trigger
        result = get_persona(minutes_since_last=10, timer_trigger=False)
        assert "主動聯絡" not in result
        assert "一整天" not in result

    def test_timer_trigger_takes_precedence(self):
        # timer_trigger=True should add timer rule even with high absence minutes
        result = get_persona(timer_trigger=True, minutes_since_last=600)
        assert "主動聯絡" in result


# ------------------------------------------------------------------
# get_persona — news injection
# ------------------------------------------------------------------
class TestGetPersonaNewsInjection:
    def test_news_injected_when_provided(self):
        news_content = "Breaking: Something happened!"
        result = get_persona(news=news_content)
        assert news_content in result
        assert "新聞內容" in result

    def test_news_rules_included_with_news(self):
        result = get_persona(news="Some news")
        assert "外部情報注入" in result

    def test_no_news_block_when_news_empty(self):
        result = get_persona(news="")
        assert "新聞內容" not in result
        assert "外部情報注入" not in result


# ------------------------------------------------------------------
# get_persona — Redis full template override
# ------------------------------------------------------------------
class TestGetPersonaRedisOverride:
    def test_custom_base_identity_from_redis(self):
        redis = MagicMock()
        custom_template = {"base_identity": "## Custom AI Identity\nI am CustomBot."}
        redis.get.return_value = json.dumps(custom_template)
        result = get_persona(redis_client=redis)
        assert "CustomBot" in result
        # Original base identity should NOT be present
        assert "莉莉絲" not in result

    def test_custom_style_from_redis(self):
        redis = MagicMock()
        custom_template = {"style_normal": "## Custom Style\nCustom rules here."}
        redis.get.return_value = json.dumps(custom_template)
        result = get_persona(length_mode="normal", redis_client=redis)
        assert "Custom rules here." in result

    def test_partial_override_falls_back_to_defaults(self):
        redis = MagicMock()
        # Only override base_identity, style should still use default
        custom_template = {"base_identity": "Override identity"}
        redis.get.return_value = json.dumps(custom_template)
        result = get_persona(length_mode="short", redis_client=redis)
        assert "Override identity" in result
        assert "省流" in result  # Default STYLE_SHORT still present
