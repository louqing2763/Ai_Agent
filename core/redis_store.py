import json
import redis

def init_redis(url=None, host=None, port=None, password=None):
    try:
        if url:
            r = redis.from_url(url, decode_responses=True)
            r.ping()
            return r
        r = redis.Redis(
            host=host,
            port=port,
            password=password,
            decode_responses=True,
        )
        r.ping()
        return r
    except:
        return None

fallback = {"history": {}, "state": {}}

def save_history(cid, data, redis_client):
    data = data[-40:]
    if redis_client:
        try:
            redis_client.set(f"history:{cid}", json.dumps(data))
            return
        except:
            pass
    fallback["history"][cid] = data

def load_history(cid, redis_client):
    if redis_client:
        try:
            raw = redis_client.get(f"history:{cid}")
            if raw:
                return json.loads(raw)
        except:
            pass
    return fallback["history"].get(cid, [])
  
