import json
import logging
import threading
import redis

logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# Redis Init + Fallback
# ----------------------------------------------------------

# 用於向量操作的 raw 連線（不做 decode_responses）
_raw_redis_client = None

def init_redis(REDIS_URL=None, REDISHOST=None, REDISPORT=6379, REDISPASSWORD=None):
    global _raw_redis_client
    try:
        if REDIS_URL:
            r = redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            # 建立不帶 decode_responses 的連線，供向量操作使用
            _raw_redis_client = redis.from_url(REDIS_URL, decode_responses=False)
            logger.info("✅ Redis connected via REDIS_URL")
            return r

        r = redis.Redis(
            host=REDISHOST,
            port=REDISPORT,
            password=REDISPASSWORD if REDISPASSWORD else None,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        r.ping()
        # 建立不帶 decode_responses 的連線，供向量操作使用
        _raw_redis_client = redis.Redis(
            host=REDISHOST,
            port=REDISPORT,
            password=REDISPASSWORD if REDISPASSWORD else None,
            decode_responses=False,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        logger.info("✅ Redis connected")
        return r

    except Exception as e:
        logger.error(f"❌ Redis failed, fallback: {e}")
        return None


def get_raw_redis():
    """取得不帶 decode_responses 的 Redis 連線，供向量操作使用。"""
    return _raw_redis_client


# Fallback (when Redis unavailable) — thread-safe
_fallback_lock = threading.Lock()
fallback = {
    "history": {},
    "state": {},
}

# ----------------------------------------------------------
# History Save / Load
# ----------------------------------------------------------

def save_history(cid, history, redis_client=None):
    history = history[-40:]
    if redis_client:
        try:
            redis_client.set(f"history:{cid}", json.dumps(history))
            return
        except Exception as e:
            logger.warning(f"[redis] save_history 失敗，用 fallback: {e}")

    with _fallback_lock:
        fallback["history"][cid] = history


def load_history(cid, redis_client=None):
    if redis_client:
        try:
            raw = redis_client.get(f"history:{cid}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"[redis] load_history 失敗，用 fallback: {e}")

    with _fallback_lock:
        return fallback["history"].get(cid, [])


# ----------------------------------------------------------
# State Save / Load
# ----------------------------------------------------------

def save_state(cid, state, redis_client=None):
    if redis_client:
        try:
            redis_client.set(f"state:{cid}", json.dumps(state))
            return
        except Exception as e:
            logger.warning(f"[redis] save_state 失敗，用 fallback: {e}")

    with _fallback_lock:
        fallback["state"][cid] = state


def load_state(cid, redis_client=None):
    if redis_client:
        try:
            raw = redis_client.get(f"state:{cid}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning(f"[redis] load_state 失敗，用 fallback: {e}")

    with _fallback_lock:
        return fallback["state"].get(cid, {
            "voice_mode": False,
            "sleeping": False,
            "active": 0,
            "news_cache": ""
        })

