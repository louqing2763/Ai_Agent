import re

def analyze_emotion(user_text):
    """
    分析使用者語氣情緒 → low / neutral / high
    """

    low_words = ["難過", "累", "不想", "沒力氣", "孤單", "壓力", "討厭自己", "痛"]
    high_words = ["開心", "哈哈", "好耶", "嗨", "太棒", "喜歡", "爽"]

    score = 0

    for w in low_words:
        if w in user_text:
            score -= 1

    for w in high_words:
        if w in user_text:
            score += 1

    if score <= -1:
        return "low"
    if score >= 1:
        return "high"
    return "neutral"


def compute_gesture_level(emotion):
    """
    emotion → gesture 等級 (1~3)
    """
    if emotion == "low":
        return 1
    elif emotion == "high":
        return 3
    return 2


def compute_affinity(previous_affinity, user_text):
    """
    根據使用者語氣調整親密度 (0.0~1.0)
    """

    affinity = previous_affinity

    # 🩹 使用者文本越短 → AI 自動靠近你（因為你看起來沒有精神）
    if len(user_text) < 6:
        affinity += 0.03

    # 🩹 撒嬌、情感字詞 → 增加親密度
    if any(w in user_text for w in ["想你", "喜歡", "陪我", "抱", "靠", "在嗎"]):
        affinity += 0.05

    # 🩹 情緒低 → 啟動保護模式
    if any(w in user_text for w in ["難過", "累", "不舒服", "不想"]):
        affinity += 0.05

    affinity = min(1.0, max(0.0, affinity))
    return affinity


def compute_reply_length(user_text):
    """
    使用者講話長度 → AI 回覆長度
    """
    if len(user_text) < 6:
        return "short"
    if len(user_text) < 25:
        return "normal"
    return "long"


def generate_AER(user_text, state):
    """
    回傳 AER 結構：
    {
        "emotion": "low / neutral / high",
        "gesture": 1~3,
        "affinity": float,
        "length": "short / normal / long"
    }
    """

    emotion = analyze_emotion(user_text)
    affinity = compute_affinity(state.get("affinity", 0.5), user_text)
    gesture = compute_gesture_level(emotion)
    length = compute_reply_length(user_text)

    return {
        "emotion": emotion,
        "gesture": gesture,
        "affinity": affinity,
        "length": length
    }
