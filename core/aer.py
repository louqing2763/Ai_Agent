# ==========================================================
#  AER V8.0 — 完全戀人情緒系統
# ==========================================================

import time

def detect_emotion(text):
    low = ["難過", "好累", "算了", "煩", "不想", "沒力", "孤單"]
    high = ["哈哈", "開心", "太好了", "好耶", "爽", "期待"]

    if any(w in text for w in low):
        return "low"
    if any(w in text for w in high):
        return "high"
    return "neutral"


def update_affinity(state, text):
    now = time.time()
    last = state.get("last_timestamp", now)
    aff = state.get("affinity", 1.2)

    state["last_timestamp"] = now

    # 越久沒來 → 她越不安（親密度微降）
    if now - last > 300:
        aff -= 0.03
    else:
        aff += 0.04  # 聊天會增加親密度

    # 若你說撒嬌詞 → 親密 +0.06
    if any(w in text for w in ["想你", "陪我", "抱", "靠", "喜歡你"]):
        aff += 0.06

    return max(0.6, min(2.0, aff))


def gesture_level(emotion, aff):
    if emotion == "low":
        return 2  # 更靠近你
    if emotion == "high":
        return 3  # 活潑甜
    return 2 if aff > 1.3 else 1


def reply_length(text, emotion):
    if emotion == "low":
        return "short"
    if len(text) > 25:
        return "long"
    return "normal"


def regulate(text, state):
    emo = detect_emotion(text)
    aff = update_affinity(state, text)
    gest = gesture_level(emo, aff)
    length = reply_length(text, emo)

    state["emotion"] = emo
    state["affinity"] = aff

    return {
        "emotion": emo,
        "affinity": aff,
        "gesture": gest,
        "length": length
    }
