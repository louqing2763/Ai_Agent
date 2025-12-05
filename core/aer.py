# ==========================================================
#  AER — Auto Emotion Regulator V7.4
#  新增：情緒平滑過渡、避免跳情緒、自然情緒承接
# ==========================================================

import time

# ----------------------------------------------------------
# 1. Detect Emotion
# ----------------------------------------------------------

def detect_emotion(user_text: str) -> str:
    if not user_text:
        return "neutral"

    low_words = ["好累", "煩", "算了", "不知道", "沒事", "唉", "不想", "累", "難過"]
    high_words = ["哈哈", "爽爽", "太好了", "好耶", "開心", "興奮"]

    if any(w in user_text for w in low_words):
        return "low"
    if any(w in user_text for w in high_words):
        return "high"
    return "neutral"


# ----------------------------------------------------------
# 2. Update Affinity
# ----------------------------------------------------------

def update_affinity(state: dict):
    now = time.time()

    last = state.get("last_timestamp", None)
    affinity = state.get("affinity", 1.0)

    state["last_timestamp"] = now

    # 距離上一句很久 → 微降
    if last:
        diff = now - last
        if diff > 300:
            affinity -= 0.03
        else:
            affinity += 0.03

    affinity = max(0.5, min(2.0, affinity))
    state["affinity"] = affinity
    return affinity


# ----------------------------------------------------------
# 3. Gesture Level
# ----------------------------------------------------------

def gesture_level(emotion: str, affinity: float):
    if emotion == "low":
        return 1
    if emotion == "high":
        return 2 if affinity < 1.5 else 3

    # neutral
    if affinity >= 1.7:
        return 2
    return 1


# ----------------------------------------------------------
# 4. Reply Length
# ----------------------------------------------------------

def reply_length(user_text: str, emotion: str):
    if emotion == "low":
        return "short"
    if len(user_text) > 25:
        return "long"
    return "normal"


# ----------------------------------------------------------
# 5. Smooth Emotion Transition（最重要）
# ----------------------------------------------------------

def smooth_emotion(prev: str, now: str):
    # 高 → 低 / 低 → 高 → 轉成 neutral 過渡
    if prev == "low" and now == "high":
        return "neutral"
    if prev == "high" and now == "low":
        return "neutral"

    return now


# ----------------------------------------------------------
# 6. Master AER Function
# ----------------------------------------------------------

def regulate(user_text: str, state: dict):
    raw_emotion = detect_emotion(user_text)
    prev_emotion = state.get("emotion", "neutral")

    emotion = smooth_emotion(prev_emotion, raw_emotion)
    affinity = update_affinity(state)
    gesture = gesture_level(emotion, affinity)
    length = reply_length(user_text, emotion)

    # 記錄下次使用
    state["emotion"] = emotion

    return {
        "emotion": emotion,
        "affinity": affinity,
        "gesture": gesture,
        "length": length
    }
