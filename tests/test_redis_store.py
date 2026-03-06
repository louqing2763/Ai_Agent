"""
tests/test_redis_store.py — Unit tests for core/redis_store.py

Coverage areas:
- save_history / load_history: with and without a Redis client
- save_state / load_state: with and without a Redis client
- History trimming to 40 messages
- Default state values when key is absent
- Redis errors fall back to in-memory storage
"""

import json
import pytest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import redis_store
from core.redis_store import save_history, load_history, save_state, load_state


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def make_messages(n: int) -> list:
    return [{"role": "user", "content": f"msg {i}"} for i in range(n)]


def reset_fallback():
    """Clear the in-memory fallback dict between tests."""
    redis_store.fallback["history"].clear()
    redis_store.fallback["state"].clear()


# ------------------------------------------------------------------
# save_history / load_history — No Redis (fallback)
# ------------------------------------------------------------------
class TestHistoryFallback:
    def setup_method(self):
        reset_fallback()

    def test_save_and_load_round_trip(self):
        history = make_messages(5)
        save_history("user1", history)
        loaded = load_history("user1")
        assert loaded == history

    def test_load_nonexistent_key_returns_empty_list(self):
        assert load_history("unknown_user") == []

    def test_history_trimmed_to_40_messages(self):
        history = make_messages(50)
        save_history("user1", history)
        loaded = load_history("user1")
        assert len(loaded) == 40
        # Should keep the LAST 40 messages
        assert loaded[0]["content"] == "msg 10"
        assert loaded[-1]["content"] == "msg 49"

    def test_overwrite_existing_history(self):
        save_history("user1", make_messages(3))
        new_history = [{"role": "assistant", "content": "new"}]
        save_history("user1", new_history)
        assert load_history("user1") == new_history

    def test_multiple_users_isolated(self):
        save_history("user1", [{"role": "user", "content": "a"}])
        save_history("user2", [{"role": "user", "content": "b"}])
        assert load_history("user1")[0]["content"] == "a"
        assert load_history("user2")[0]["content"] == "b"


# ------------------------------------------------------------------
# save_history / load_history — With Redis
# ------------------------------------------------------------------
class TestHistoryWithRedis:
    def setup_method(self):
        reset_fallback()
        self.redis = MagicMock()

    def test_save_calls_redis_set(self):
        history = make_messages(3)
        save_history("user1", history, redis_client=self.redis)
        self.redis.set.assert_called_once_with("history:user1", json.dumps(history))

    def test_load_calls_redis_get(self):
        history = make_messages(3)
        self.redis.get.return_value = json.dumps(history)
        result = load_history("user1", redis_client=self.redis)
        self.redis.get.assert_called_once_with("history:user1")
        assert result == history

    def test_load_returns_empty_list_when_redis_key_missing(self):
        self.redis.get.return_value = None
        result = load_history("user1", redis_client=self.redis)
        assert result == []

    def test_redis_save_error_falls_back_to_memory(self):
        self.redis.set.side_effect = Exception("Redis down")
        history = make_messages(2)
        save_history("user1", history, redis_client=self.redis)
        # Should have fallen back to in-memory
        assert redis_store.fallback["history"]["user1"] == history

    def test_redis_load_error_falls_back_to_memory(self):
        self.redis.get.side_effect = Exception("Redis down")
        redis_store.fallback["history"]["user1"] = make_messages(2)
        result = load_history("user1", redis_client=self.redis)
        assert len(result) == 2

    def test_history_trimmed_before_redis_save(self):
        history = make_messages(50)
        save_history("user1", history, redis_client=self.redis)
        saved_data = json.loads(self.redis.set.call_args[0][1])
        assert len(saved_data) == 40


# ------------------------------------------------------------------
# save_state / load_state — No Redis (fallback)
# ------------------------------------------------------------------
class TestStateFallback:
    def setup_method(self):
        reset_fallback()

    def test_save_and_load_round_trip(self):
        state = {"voice_mode": True, "sleeping": False, "active": 12345}
        save_state("user1", state)
        loaded = load_state("user1")
        assert loaded == state

    def test_load_missing_key_returns_defaults(self):
        state = load_state("unknown_user")
        assert state["voice_mode"] is False
        assert state["sleeping"] is False
        assert state["active"] == 0
        assert state["news_cache"] == ""

    def test_overwrite_state(self):
        save_state("user1", {"voice_mode": False})
        save_state("user1", {"voice_mode": True})
        assert load_state("user1")["voice_mode"] is True


# ------------------------------------------------------------------
# save_state / load_state — With Redis
# ------------------------------------------------------------------
class TestStateWithRedis:
    def setup_method(self):
        reset_fallback()
        self.redis = MagicMock()

    def test_save_calls_redis_set(self):
        state = {"voice_mode": True}
        save_state("user1", state, redis_client=self.redis)
        self.redis.set.assert_called_once_with("state:user1", json.dumps(state))

    def test_load_calls_redis_get(self):
        state = {"voice_mode": True, "sleeping": False, "active": 0, "news_cache": ""}
        self.redis.get.return_value = json.dumps(state)
        result = load_state("user1", redis_client=self.redis)
        assert result == state

    def test_load_returns_defaults_when_redis_key_missing(self):
        self.redis.get.return_value = None
        result = load_state("user1", redis_client=self.redis)
        assert result["voice_mode"] is False
        assert result["sleeping"] is False

    def test_redis_save_error_falls_back_to_memory(self):
        self.redis.set.side_effect = Exception("connection lost")
        state = {"voice_mode": True}
        save_state("user1", state, redis_client=self.redis)
        assert redis_store.fallback["state"]["user1"] == state

    def test_redis_load_error_falls_back_to_memory(self):
        self.redis.get.side_effect = Exception("connection lost")
        redis_store.fallback["state"]["user1"] = {"voice_mode": True}
        result = load_state("user1", redis_client=self.redis)
        assert result["voice_mode"] is True
