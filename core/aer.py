# ==========================================================
#   AER — Auto Emotion Regulator V7.8
#   Mode: 60% 新戀人 × 40% 舊溫柔
# ==========================================================

import time
import random


# ----------------------------------------------------------
# 1. 基本情緒辨識（新版：增加戀人情感詞）
# ----------------------------------------------------------

def detect_emotion(user_text: str) -> str:
    if not user_text:
        return "neutral"

    low_words = ["好累", "難過", "煩", "算了", "不想", "沒力氣", "心好痛"]
    high_words = ["哈哈", "太好了", "開心", "好耶", "爽", "興奮"]
    love_trigger = ["想你", "喜歡你", "好可愛", "抱我"]

    if any(w in user_text for w in low_words):
        return "low"
    if any(w in user_text for w in high_words):
        return "high"
    if any(w in user_text for w in love_trigger):
        return "high"

    return "neutral"


# ----------------------------------------------------------
# 2. 親密度調整（新版：戀人行為模型）
# ----------------------------------------------------------

def update_affinity(state: dict, user_text: str):
    """
    affinity:
    0.5 = 冷淡、克制
    1.0 = 正常親密
    1.5 = 靠近、撒嬌
    2.0 = 高依戀（保留，不常達到）
    """

    now = time.time()
    last = state.get("last_timestamp", None)
    affinity = state.get("affinity", 1.0)

    state["last_timestamp"] = now

    # — 使用者長時間未出現（> 5 分鐘）
    if last and now - last > 300:
        affinity -= 0.05   # 微微變得內斂

    # — 使用者一直在對話
    elif last and now - last < 90:
        affinity += 0.04   # 輕微上升

    # — 撒嬌詞觸發
    if any(w in user_text for w in ["想你", "喜歡", "可愛", "想抱", "陪我"]):
        affinity += 0.08

    # — 使用者情緒低 → 恋人會更靠近
    if any(w in user_text for w in ["累", "難過", "不舒服", "痛"]):
        affinity += 0.05

    # — 不讓依戀值失控
    affinity = max(0.5, min(1.8, affinity))
    state["affinity"] = affinity

    return affinity


# ----------------------------------------------------------
# 3. 動作強度（C 模式：偶爾）
# ----------------------------------------------------------

def gesture_level(emotion: str, affinity: float):
    """
    gesture:
    1 = 幾乎不動
    2 = 偶爾動作（C 模式）
    3 = 更多肢體（在高親密＋高情緒時）
    """

    if emotion == "low":
        return 1  # 安靜＋靠近，不做大動作
    
    if emotion == "high":
        return 2 if affinity < 1.4 else 3

    # neutral
    return 2 if affinity >= 1.2 else 1


# ----------------------------------------------------------
# 4. 回覆長度（戀人模式 60%）
# ----------------------------------------------------------

def reply_length(user_text: str, emotion: str):
    # 情緒低 → 簡短柔聲安撫
    if emotion == "low":
        return "short"

    # 當使用者情緒正常時 → normal 或 long
    if len(user_text) > 20:
        return "long"

    return "normal"


# ----------------------------------------------------------
# 5. 情緒平滑過渡（避免跳戲、角色崩壞）
# ----------------------------------------------------------

def smooth_emotion(prev: str, now: str):
    # 避免 high ↔ low 突然切換
    if prev == "low" and now == "high":
        return "neutral"
    if prev == "high" and now == "low":
        return "neutral"

    return now


# ----------------------------------------------------------
# 6. 恋人情緒擴散（60% 新 × 40% 舊）
# ----------------------------------------------------------

def lover_bias(emotion: str, affinity: float):
    """
    新版（60%）：
        emotion 會因為 affinity 而更靠戀人方向
    舊版（40%）：
        保留溫柔穩定，不會太甜或太黏
    """

    # 親密度高 → 情緒更柔、甜
    if affinity > 1.4:
        if emotion == "neutral":
            return "soft_high"     # 高親密時的柔甜
        if emotion == "high":
            return "sweet_high"    # 輕輕甜的 high

    # 親密度低 → 稍微內斂（舊人格）
    if affinity < 0.8:
        return "soft_neutral"

    return emotion


# ----------------------------------------------------------
# 7. 主控制（整合所有 AER 子系統）
# ----------------------------------------------------------

def regulate(user_text: str, state: dict):
    raw_emotion = detect_emotion(user_text)
    prev_emotion = state.get("emotion", "neutral")

    # 1. 情緒平滑過渡
    emotion = smooth_emotion(prev_emotion, raw_emotion)

    # 2. 親密度更新（戀人 60% 行為影響）
    affinity = update_affinity(state, user_text)

    # 3. 動作強度（C 模式）
    gesture = gesture_level(emotion, affinity)

    # 4. 回覆長度
    length = reply_length(user_text, emotion)

    # 5. 恋人偏移（讓語氣更像戀人、但不崩）
    state["lover_emotion"] = lover_bias(emotion, affinity)

    # 更新狀態供 persona.py 使用
    state["emotion"] = emotion

    return {
        "emotion": emotion,
        "affinity": affinity,
        "gesture": gesture,
        "length": length
    }
