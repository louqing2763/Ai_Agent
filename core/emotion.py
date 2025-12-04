# ==========================================================
#  AER — Auto Emotion Regulator
#  自動情緒 & 親密度調節器
# ==========================================================

import time

# ----------------------------------------------------------
# 1. 情緒偵測（根據使用者訊息）
# ----------------------------------------------------------

def detect_emotion(user_text: str) -> str:
    if not user_text:
        return "neutral"

    low_words = ["好累", "煩", "算了", "不知道", "沒事", "唉", "不想", "累"]
    high_words = ["哈哈", "好爽", "太好了", "好耶", "開心"]
    
    if any(w in user_text for w in low_words):
        return "low"
    if any(w in user_text for w in high_words):
        return "high"
    return "neutral"


# ----------------------------------------------------------
# 2. 親密度計算（根據聊天頻率/沉默時間）
# ----------------------------------------------------------

def update_affinity(state: dict):
    now = time.time()

    last = state.get("last_timestamp", None)
    affinity = state.get("affinity", 1.0)  # 1.0 = baseline
    
    state["last_timestamp"] = now

    if last:
        diff = now - last

        # 很久沒講話 → affinity 降低一點（但不會降太多）
        if diff > 300:
            affinity -= 0.05
        # 常常講話 → affinity 上升
        else:
            affinity += 0.05

    # 限制數值範圍
    affinity = max(0.5, min(2.0, affinity))
    state["affinity"] = affinity

    return affinity


# ----------------------------------------------------------
# 3. 動作描寫強度調節（0～3）
# ----------------------------------------------------------

def gesture_level(emotion: str, affinity: float) -> int:

    if emotion == "low":
        return 1
    if emotion == "high":
        return 2 if affinity < 1.5 else 3

    # neutral
    if affinity >= 1.7:
        return 2
    return 1


# ----------------------------------------------------------
# 4. 回覆長度調節（簡短/正常/長）
# ----------------------------------------------------------

def reply_length(user_text: str, emotion: str):
    if emotion == "low":
        return "short"
    if len(user_text) > 25:
        return "long"
    return "normal"


# ----------------------------------------------------------
# 5. 打包整體結果
# ----------------------------------------------------------

def regulate(user_text: str, state: dict):
    emotion = detect_emotion(user_text)
    affinity = update_affinity(state)
    gesture = gesture_level(emotion, affinity)
    length = reply_length(user_text, emotion)

    return {
        "emotion": emotion,
        "affinity": affinity,
        "gesture": gesture,
        "length": length
    }
