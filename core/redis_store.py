import json
import redis

# ----------------------------------------------------------
# Redis Init + Fallback
# ----------------------------------------------------------

def init_redis(REDIS_URL=None, REDISHOST=None, REDISPORT=6379, REDISPASSWORD=None):
    try:
        if REDIS_URL:
            r = redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()
            print("✅ Redis connected via REDIS_URL")
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
        print("✅ Redis connected")
        return r

    except Exception as e:
        print("❌ Redis failed, fallback:", e)
        return None


# Fallback (when Redis unavailable)
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
        except:
            pass

    fallback["history"][cid] = history


def load_history(cid, redis_client=None):
    if redis_client:
        try:
            raw = redis_client.get(f"history:{cid}")
            if raw:
                return json.loads(raw)
        except:
            pass

    return fallback["history"].get(cid, [])


# ----------------------------------------------------------
# State Save / Load
# ----------------------------------------------------------

def save_state(cid, state, redis_client=None):
    if redis_client:
        try:
            redis_client.set(f"state:{cid}", json.dumps(state))
            return
        except:
            pass

    fallback["state"][cid] = state


def load_state(cid, redis_client=None):
    if redis_client:
        try:
            raw = redis_client.get(f"state:{cid}")
            if raw:
                return json.loads(raw)
        except:
            pass

    # default state
    return fallback["state"].get(cid, {
        "voice_mode": False,
        "sleeping": False,
        "active": 0,
        "news_cache": ""
    })
