# ==========================================================
# tts.py — 日文語音合成（ElevenLabs）
# ==========================================================

import io
import re
import requests

def clean_jp(text):
    """移除中文，只保留日文與假名"""
    text = re.sub(r"[\u4e00-\u9fff]", "", text)
    return text.strip()

def tts_jp(text, api_key, voice_id):
    jp = clean_jp(text)
    if not jp:
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {"text": jp, "model_id": "eleven_multilingual_v2"}

    resp = requests.post(
        url,
        json=payload,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json"
        }
    )

    if resp.status_code == 200:
        return io.BytesIO(resp.content)

    return None
