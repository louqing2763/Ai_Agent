import re

def analyze_user_message(text: str):
    """
    將使用者訊息分析成 AER（Auto Emotion Regulator）狀態。
    輸出格式：
    {
        "emotion": "low / neutral / high",
        "affinity": float,
        "gesture": 0~3,
        "length": "short / normal / long"
    }
    """

    text = text.strip()

    # ---------------------------
    # 1. Emotion / gesture 偵測
    # ---------------------------
    low_keywords = ["累", "難過", "不想", "沒力", "不好", "算了", "唉", "煩"]
    high_keywords = ["哈哈", "好耶", "太棒", "超爽", "興奮", "快樂", "nice", "好開心"]

    emotion = "neutral"
    gesture = 1

    if any(k in text for k in low_keywords):
        emotion = "low"
        gesture = 1
    elif any(k in text for k in high_keywords):
        emotion = "high"
        gesture = 3
    else:
        emotion = "neutral"
        gesture = 2

    # ---------------------------
    # 2. 親密度（affinity）
    # ---------------------------
    affinity = 1.0

    if "聰音" in text or "你" in text:
        affinity += 0.2
    if "想你" in text or "喜歡" in text:
        affinity += 0.4
    if "抱" in text or "陪" in text:
        affinity += 0.3

    # clamp
    affinity = min(2.0, max(0.6, affinity))

    # ---------------------------
    # 3. 回覆長度判斷
    # ---------------------------
    if len(text) < 6:
        length = "short"
    elif len(text) < 20:
        length = "normal"
    else:
        length = "long"

    return {
        "emotion": emotion,
        "affinity": affinity,
        "gesture": gesture,
        "length": length
    }
