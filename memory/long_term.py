"""
memory/long_term.py — Redis 向量長期記憶模組

Redis 8 原生支援向量搜尋，不需要額外套件。
使用 HNSW 索引 + cosine 相似度。
Embedding 使用本地 sentence-transformers，完全免費，無需 API Key。

流程：
  save(chat_id, role, content)
    → sentence-transformers 向量化
    → 存入 Redis Hash + 向量索引

  recall(chat_id, query, top_k)
    → 向量化 query
    → Redis 向量搜尋
    → 回傳格式化字串注入 system prompt
"""

import os
import time
import logging
import hashlib
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)

EMBED_DIM       = 384     # paraphrase-multilingual-MiniLM-L12-v2 的維度
INDEX_NAME      = "lilith_memory_idx"
KEY_PREFIX      = "mem"
MIN_RELEVANCE   = 0.75
MAX_RECALL      = 4
MIN_CONTENT_LEN = 10

# 模型單例（首次呼叫時載入，之後快取）
_embed_model = None

def _get_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("[memory] 載入 embedding 模型中…")
        _embed_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        logger.info("[memory] embedding 模型載入完成")
    return _embed_model


# ----------------------------------------------------------
# 🔢 Embedding（本地 sentence-transformers）
# ----------------------------------------------------------
def _embed(text: str) -> Optional[list]:
    """將文字向量化，回傳 float list。"""
    try:
        model  = _get_model()
        vector = model.encode(text[:2000], normalize_embeddings=True).tolist()
        return vector
    except Exception as e:
        logger.error(f"[memory] Embedding 失敗: {e}")
        return None





# ----------------------------------------------------------
# 🗂️ Redis 索引初始化
# ----------------------------------------------------------
def ensure_index(redis_client) -> bool:
    """
    確保向量索引存在。
    Redis 8 使用 FT.CREATE 建立 HNSW 向量索引。
    若索引已存在則跳過。
    """
    if redis_client is None:
        return False
    try:
        # 檢查索引是否存在
        redis_client.execute_command("FT.INFO", INDEX_NAME)
        return True
    except Exception:
        pass

    # 建立索引
    try:
        redis_client.execute_command(
            "FT.CREATE", INDEX_NAME,
            "ON", "HASH",
            "PREFIX", "1", f"{KEY_PREFIX}:",
            "SCHEMA",
            "chat_id",   "TAG",
            "role",      "TAG",
            "ts",        "NUMERIC", "SORTABLE",
            "content",   "TEXT",
            "embedding", "VECTOR", "HNSW", "6",
                "TYPE",            "FLOAT32",
                "DIM",             str(EMBED_DIM),
                "DISTANCE_METRIC", "COSINE",
        )
        logger.info(f"[memory] Redis 向量索引 '{INDEX_NAME}' 建立成功")
        return True
    except Exception as e:
        logger.error(f"[memory] 建立索引失敗: {e}")
        return False


# ----------------------------------------------------------
# 💾 儲存長期記憶
# ----------------------------------------------------------
def save(redis_client, chat_id: int, role: str, content: str) -> bool:
    """
    將一條對話存入長期記憶向量庫。

    Args:
        redis_client: Redis 連線
        chat_id:      Telegram chat ID
        role:         "user" 或 "assistant"
        content:      訊息內容

    Returns:
        True = 成功
    """
    if redis_client is None:
        return False

    # 過濾不值得記憶的內容
    content = content.strip()
    if len(content) < MIN_CONTENT_LEN:
        return False
    if content.startswith("(System") or content.startswith("（OOC"):
        return False

    # 向量化
    vector = _embed(content)
    if vector is None:
        return False

    # 建立唯一 ID（防重複）
    ts     = int(time.time())
    uid    = hashlib.md5(f"{chat_id}:{role}:{ts}:{content[:50]}".encode()).hexdigest()[:12]
    key    = f"{KEY_PREFIX}:{uid}"

    # 轉成 Redis 可存的格式
    import struct
    vector_bytes = struct.pack(f"{EMBED_DIM}f", *vector)

    try:
        redis_client.hset(key, mapping={
            "chat_id":   str(chat_id),
            "role":      role,
            "ts":        ts,
            "content":   content[:500],    # 最多存 500 字
            "embedding": vector_bytes,
        })
        # 設定 TTL：記憶保留 90 天
        redis_client.expire(key, 86400 * 90)
        logger.debug(f"[memory] 已存入: {key}")
        return True
    except Exception as e:
        logger.error(f"[memory] 寫入失敗: {e}")
        return False


# ----------------------------------------------------------
# 🔍 撷取相關長期記憶
# ----------------------------------------------------------
def recall(redis_client, chat_id: int, query: str, top_k: int = MAX_RECALL) -> str:
    """
    從長期記憶撷取與 query 語意最相關的歷史片段。

    Returns:
        可直接注入 system prompt 的字串；無結果回傳空字串。
    """
    if redis_client is None or not query.strip():
        return ""

    # 向量化 query
    q_vector = _embed(query)
    if q_vector is None:
        return ""

    import struct
    q_bytes = struct.pack(f"{EMBED_DIM}f", *q_vector)

    try:
        # KNN 向量搜尋（只搜此 chat 的記憶）
        results = redis_client.execute_command(
            "FT.SEARCH", INDEX_NAME,
            f"(@chat_id:{{{chat_id}}})=>[KNN {top_k} @embedding $vec AS score]",
            "PARAMS", "2", "vec", q_bytes,
            "RETURN", "4", "content", "role", "ts", "score",
            "SORTBY", "score",
            "DIALECT", "2",
        )
    except Exception as e:
        logger.error(f"[memory] 向量搜尋失敗: {e}")
        return ""

    if not results or results[0] == 0:
        return ""

    # 解析結果
    # results 格式：[total, key1, [field, val, ...], key2, ...]
    total   = results[0]
    entries = results[1:]

    fragments = []
    for i in range(0, len(entries), 2):
        fields = {}
        raw    = entries[i + 1]
        for j in range(0, len(raw), 2):
            fields[raw[j]] = raw[j + 1]

        score   = float(fields.get("score", 1.0))
        relevance = 1.0 - score   # cosine distance → similarity

        if relevance < MIN_RELEVANCE:
            continue

        content  = fields.get("content", "")
        role     = fields.get("role", "user")
        ts       = int(fields.get("ts", 0))
        label    = "你說過" if role == "user" else "莉莉絲說過"
        time_str = _time_ago(ts)

        fragments.append((ts, f"• {time_str}，{label}：「{content[:120]}」"))

    if not fragments:
        return ""

    # 按時間排序（讓 LLM 有時序感）
    fragments.sort(key=lambda x: x[0])
    lines = ["[長期記憶·相關片段]"] + [f[1] for f in fragments]
    return "\n".join(lines)


# ----------------------------------------------------------
# 🗑️ 刪除記憶
# ----------------------------------------------------------
def delete_all(redis_client, chat_id: int) -> int:
    """刪除某 chat 的所有長期記憶，回傳刪除條數。"""
    if redis_client is None:
        return 0
    try:
        results = redis_client.execute_command(
            "FT.SEARCH", INDEX_NAME,
            f"@chat_id:{{{chat_id}}}",
            "RETURN", "0",
            "LIMIT", "0", "1000",
        )
        keys = results[1::2]   # 取奇數位置的 key
        if keys:
            redis_client.delete(*keys)
        return len(keys)
    except Exception as e:
        logger.error(f"[memory] 刪除失敗: {e}")
        return 0


def count(redis_client, chat_id: int) -> int:
    """回傳某 chat 的長期記憶條數。"""
    if redis_client is None:
        return 0
    try:
        results = redis_client.execute_command(
            "FT.SEARCH", INDEX_NAME,
            f"@chat_id:{{{chat_id}}}",
            "RETURN", "0",
            "LIMIT", "0", "0",
        )
        return results[0] if results else 0
    except Exception:
        return 0


# ----------------------------------------------------------
# 🕐 工具
# ----------------------------------------------------------
def _time_ago(ts: int) -> str:
    if not ts:
        return "某個時候"
    diff = int(time.time()) - ts
    if diff < 3600:   return f"{diff // 60} 分鐘前"
    if diff < 86400:  return f"{diff // 3600} 小時前"
    if diff < 604800: return f"{diff // 86400} 天前"
    return f"{diff // 604800} 週前"
