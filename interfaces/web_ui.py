"""
interfaces/web_ui.py — FastAPI Web UI v4.0

新增：
  - 通話模式（雙向語音）
    - /chat/stream  — SSE 串流回覆
    - /tts/proxy    — 代理本機 GPT-SoVITS（可選，瀏覽器也能直連）
  - VAD（語音活動偵測）+ 麥克風輸入 → Whisper STT
  - 前端通話 UI：一鍵進入通話狀態，波形動畫，即時播放
"""

import time
import json
import logging
import asyncio
from datetime import datetime
from typing import Optional, AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# GPT-SoVITS 本機地址（瀏覽器直連，後端不需要代理）
SOVITS_URL      = "http://127.0.0.1:9880/tts"
SOVITS_REF_WAV  = "C:/wav_ready/vo_cn_lilith_606.wav"
SOVITS_PROMPT   = "这是负担最小的做法了，你很快就会没事的"

# ----------------------------------------------------------
# 📦 Request 模型
# ----------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None

class StreamRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None

class SettingsRequest(BaseModel):
    length_mode: Optional[str] = None

class PersonaBlock(BaseModel):
    base_identity:  Optional[str] = None
    style_short:    Optional[str] = None
    style_normal:   Optional[str] = None
    style_long:     Optional[str] = None
    time_rules:     Optional[str] = None
    absence_rules:  Optional[str] = None
    news_rules:     Optional[str] = None

# ----------------------------------------------------------
# 🔑 Persona Redis Key
# ----------------------------------------------------------
PERSONA_KEY = "lilith:persona_full_template"

def _load_persona_blocks(redis_client) -> dict:
    if redis_client is None:
        return {}
    try:
        raw = redis_client.get(PERSONA_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _save_persona_blocks(redis_client, blocks: dict):
    if redis_client is None:
        return
    try:
        redis_client.set(PERSONA_KEY, json.dumps(blocks, ensure_ascii=False))
    except Exception as e:
        logger.error(f"[web_ui] persona 儲存失敗: {e}")

def _get_default_blocks() -> dict:
    try:
        from core.persona_config import (
            BASE_IDENTITY, STYLE_SHORT, STYLE_NORMAL, STYLE_LONG,
            TIME_RULES, ABSENCE_RULES, NEWS_RULES,
        )
        return {
            "base_identity": BASE_IDENTITY.strip(),
            "style_short":   STYLE_SHORT.strip(),
            "style_normal":  STYLE_NORMAL.strip(),
            "style_long":    STYLE_LONG.strip(),
            "time_rules":    "\n".join(TIME_RULES.values()).strip(),
            "absence_rules": "\n".join(ABSENCE_RULES.values()).strip(),
            "news_rules":    NEWS_RULES.strip(),
        }
    except Exception:
        return {k: "" for k in [
            "base_identity","style_short","style_normal",
            "style_long","time_rules","absence_rules","news_rules"
        ]}

# ----------------------------------------------------------
# 🏗️ App 工廠
# ----------------------------------------------------------
def create_app(admin_id: int, redis_client, deepseek_key: str) -> FastAPI:
    app = FastAPI(title="Lilith Agent", version="4.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    # ── 聊天（標準） ──────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _html()

    @app.post("/chat")
    async def chat(req: ChatRequest):
        from core.redis_store import load_state, save_state
        from interfaces.telegram_bot import generate_reply

        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        try:
            reply = await generate_reply(
                chat_id=admin_id, redis_client=redis_client,
                deepseek_key=deepseek_key, user_text=req.message,
            )
        except Exception as e:
            logger.error(f"[web_ui] generate_reply 失敗: {e}")
            reply = "（系統忙碌中，請稍後再試）"

        return JSONResponse({
            "reply":       reply,
            "length_mode": state.get("length_mode","normal"),
            "timestamp":   datetime.now().strftime("%H:%M"),
        })

    # ── 串流聊天（通話模式） ──────────────────────────────

    @app.post("/chat/stream")
    async def chat_stream(req: StreamRequest):
        from core.redis_store import load_state, save_state, load_history, save_history
        from core.persona_config import get_persona
        from memory.long_term import ensure_index, recall, save as mem_save
        from agent.brain import think_stream

        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        length_mode = state.get("length_mode", "normal")
        history     = load_history(admin_id, redis_client)
        news_text   = state.get("news_cache", "")

        ensure_index(redis_client)
        long_term_ctx = recall(redis_client, admin_id, query=req.message)

        persona = get_persona(
            length_mode=length_mode, news=news_text,
            redis_client=redis_client,
        )
        if long_term_ctx:
            persona += f"\n\n{long_term_ctx}\n"

        messages = [{"role": "system", "content": persona}] + history
        messages.append({"role": "user", "content": req.message})

        full_reply = []

        async def event_generator() -> AsyncGenerator[str, None]:
            async for token in think_stream(messages, length_mode):
                full_reply.append(token)
                yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

            # 背景更新記憶
            reply_text = "".join(full_reply)
            history.append({"role": "user",      "content": req.message})
            history.append({"role": "assistant",  "content": reply_text})
            trimmed = history[-40:]
            save_history(admin_id, trimmed, redis_client)
            save_state(admin_id, state, redis_client)
            asyncio.create_task(_bg_save(redis_client, admin_id, req.message, reply_text))

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _bg_save(redis_client, chat_id, user_text, reply):
        from memory.long_term import save as mem_save
        try:
            await asyncio.to_thread(mem_save, redis_client, chat_id, "user",      user_text)
            await asyncio.to_thread(mem_save, redis_client, chat_id, "assistant", reply)
        except Exception as e:
            logger.error(f"[web_ui] 背景記憶寫入失敗: {e}")

    # ── TTS 設定端點（讓前端知道參數） ───────────────────

    @app.get("/tts/config")
    async def tts_config():
        return JSONResponse({
            "url":         SOVITS_URL,
            "ref_audio":   SOVITS_REF_WAV,
            "prompt_text": SOVITS_PROMPT,
            "prompt_lang": "zh",
            "text_lang":   "zh",
        })

    # ── 其餘端點（與 v3.0 相同） ─────────────────────────

    @app.get("/history")
    async def history():
        from core.redis_store import load_history
        h = load_history(admin_id, redis_client)
        return JSONResponse({"history": h, "count": len(h)})

    @app.get("/status")
    async def status():
        from core.redis_store import load_state, load_history
        from memory.long_term import count
        state   = load_state(admin_id, redis_client)
        history = load_history(admin_id, redis_client)
        n_long  = await asyncio.to_thread(count, redis_client, admin_id)
        last_ts = state.get("last_user_timestamp", 0)
        minutes = int((time.time() - last_ts) / 60) if last_ts else 0
        return JSONResponse({
            "time":             datetime.now().strftime("%H:%M"),
            "minutes_idle":     minutes,
            "length_mode":      state.get("length_mode","normal"),
            "has_news_cache":   bool(state.get("news_cache")),
            "short_term_count": len(history),
            "long_term_count":  n_long,
        })

    @app.post("/settings")
    async def save_settings(req: SettingsRequest):
        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
            save_state(admin_id, state, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/reset")
    async def reset():
        from core.redis_store import save_history, save_state
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care": False, "length_mode": "normal",
        }, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/care")
    async def trigger_care():
        from interfaces.telegram_bot import generate_reply
        reply = await generate_reply(
            chat_id=admin_id, redis_client=redis_client,
            deepseek_key=deepseek_key,
            user_text="(System: 強制觸發主動關心)",
            timer_trigger=True, minutes_since_last=300,
        )
        return JSONResponse({"ok": True, "reply": reply})

    @app.get("/persona")
    async def get_persona_ep():
        overrides = _load_persona_blocks(redis_client)
        defaults  = _get_default_blocks()
        merged    = {k: overrides.get(k, defaults.get(k, "")) for k in defaults}
        return JSONResponse(merged)

    @app.post("/persona")
    async def save_persona(blocks: PersonaBlock):
        current  = _load_persona_blocks(redis_client)
        incoming = {k: v for k, v in blocks.dict().items() if v is not None}
        current.update(incoming)
        _save_persona_blocks(redis_client, current)
        return JSONResponse({"ok": True, "message": "Persona 已套用，下一條訊息生效"})

    @app.post("/persona/reset")
    async def reset_persona():
        if redis_client:
            try:
                redis_client.delete(PERSONA_KEY)
            except Exception:
                pass
        return JSONResponse({"ok": True, "message": "已重設為原始版本"})

    return app


# ----------------------------------------------------------
# 🎨 HTML（含通話 UI）
# ----------------------------------------------------------
def _html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉絲</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@300;400;500&family=JetBrains+Mono:wght@300;400&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0c10;--bg2:#111118;--bg3:#16161f;
  --border:#1f1f2e;--border2:#2a2a3a;
  --text:#ddd8d0;--text2:#888;--text3:#444;
  --accent:#8b7cf8;--accent2:#b09af5;--accent3:#6b5ce7;
  --danger:#e07070;--call:#5bc478;--call-active:#3da558;
}
body{font-family:'Noto Serif TC',serif;background:var(--bg);color:var(--text);height:100dvh;display:flex;overflow:hidden}

/* ── 側欄 ── */
#sidebar{width:280px;min-width:280px;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}
.s-sec{padding:14px 16px;border-bottom:1px solid var(--border)}
.s-title{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px;font-family:'JetBrains Mono',monospace}
.stat-row{display:flex;justify-content:space-between;font-size:12px;color:var(--text2);padding:3px 0;font-family:'JetBrains Mono',monospace}
.stat-val{color:var(--accent2)}
.mode-btn{width:100%;padding:8px 12px;margin-bottom:4px;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;color:var(--text2);font-size:12px;cursor:pointer;text-align:left;transition:all .15s;font-family:'Noto Serif TC',serif}
.mode-btn:hover{background:#1c1c28}
.mode-btn.active{border-color:var(--accent3);color:var(--text);background:#1a1730}
.act-btn{width:100%;padding:8px 12px;margin-bottom:4px;border-radius:6px;font-size:12px;cursor:pointer;border:none;transition:all .15s;text-align:left;font-family:'Noto Serif TC',serif}
.act-btn:hover{opacity:.8}
.btn-purple{background:#1a1730;color:var(--accent2);border:1px solid var(--border2)}
.btn-danger{background:#1e1010;color:var(--danger);border:1px solid #2e1a1a}

/* 通話按鈕 */
#callBtn{
  width:100%;padding:10px 12px;margin-bottom:4px;
  border-radius:6px;font-size:13px;cursor:pointer;
  border:1px solid #2a3e2a;background:#121e14;color:var(--call);
  font-family:'Noto Serif TC',serif;transition:all .2s;
  display:flex;align-items:center;gap:8px;
}
#callBtn:hover{background:#162018}
#callBtn.active{background:#1a3020;border-color:var(--call-active);color:#7aefa0}
#callBtn .call-dot{
  width:8px;height:8px;border-radius:50%;background:var(--call);
  transition:background .2s;flex-shrink:0;
}
#callBtn.active .call-dot{
  background:#7aefa0;
  animation:pulse-dot 1.5s infinite;
}
@keyframes pulse-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}

/* Persona 編輯 */
.p-tabs{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:8px}
.ptab{padding:3px 8px;border-radius:4px;font-size:11px;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);cursor:pointer;transition:all .15s;font-family:'JetBrains Mono',monospace}
.ptab.active{background:#1a1730;border-color:var(--accent3);color:var(--accent2)}
.p-editor{width:100%;min-height:140px;background:#0e0e18;border:1px solid var(--border2);border-radius:6px;color:#ccc;font-size:11px;font-family:'JetBrains Mono',monospace;padding:10px;resize:vertical;line-height:1.5;outline:none}
.p-editor:focus{border-color:var(--accent3)}
.p-hint{font-size:10px;color:var(--text3);margin:4px 0 7px;font-family:'JetBrains Mono',monospace}
.p-btns{display:flex;gap:5px}
.p-btns button{flex:1;padding:6px;border-radius:5px;font-size:11px;cursor:pointer;border:none;transition:opacity .15s;font-family:'Noto Serif TC',serif}
.p-btns button:hover{opacity:.85}
#btnApply{background:var(--accent3);color:#fff}
#btnReset{background:var(--bg3);color:var(--text2);border:1px solid var(--border2)}
.p-status{font-size:10px;color:var(--accent2);margin-top:5px;min-height:14px;font-family:'JetBrains Mono',monospace}

/* ── 主區 ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
header{padding:12px 18px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,var(--accent3),#c46ef7);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.h-name{font-weight:500;font-size:14px;letter-spacing:.02em}
.h-sub{font-size:11px;color:var(--text3);font-family:'JetBrains Mono',monospace}
#msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.mwrap{display:flex;flex-direction:column}
.mwrap.user{align-items:flex-end}
.mwrap.lilith{align-items:flex-start}
.mwrap.sys{align-items:center}
.bubble{max-width:72%;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word;font-weight:300}
.bubble.user{background:var(--accent3);color:#fff;border-bottom-right-radius:3px}
.bubble.lilith{background:var(--bg3);border:1px solid var(--border2);border-bottom-left-radius:3px}
.bubble.sys{background:transparent;color:var(--text3);font-size:11px;font-family:'JetBrains Mono',monospace}
.bubble code{background:var(--bg2);padding:1px 5px;border-radius:3px;font-size:11px;color:var(--accent2);font-family:'JetBrains Mono',monospace}
.ts{font-size:10px;color:var(--text3);margin-top:3px;font-family:'JetBrains Mono',monospace}
.streaming-cursor::after{content:"▋";animation:blink .7s infinite;color:var(--accent2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.typing-wrap{display:flex;align-items:flex-start}
.typing{background:var(--bg3);border:1px solid var(--border2);border-radius:14px;border-bottom-left-radius:3px;padding:10px 14px;display:flex;gap:4px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--accent3);animation:bounce 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-4px);opacity:1}}
footer{padding:10px 14px;background:var(--bg2);border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
#inp{flex:1;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:9px 13px;border-radius:14px;font-size:14px;resize:none;max-height:100px;outline:none;line-height:1.4;font-family:'Noto Serif TC',serif;font-weight:300}
#inp:focus{border-color:var(--accent3)}
#send{width:36px;height:36px;border-radius:50%;background:var(--accent3);border:none;color:#fff;font-size:14px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
#send:hover{background:var(--accent)}
#send:disabled{background:var(--bg3);cursor:not-allowed;border:1px solid var(--border2)}

/* ── 通話覆蓋層 ── */
#callOverlay{
  position:absolute;inset:0;
  background:rgba(10,10,16,.96);
  backdrop-filter:blur(12px);
  display:none;flex-direction:column;
  align-items:center;justify-content:center;
  gap:28px;z-index:50;
}
#callOverlay.active{display:flex}
.call-avatar{
  width:90px;height:90px;border-radius:50%;
  background:linear-gradient(135deg,var(--accent3),#c46ef7);
  display:flex;align-items:center;justify-content:center;
  font-size:38px;position:relative;
}
.call-ring{
  position:absolute;inset:-10px;border-radius:50%;
  border:2px solid var(--accent3);opacity:0;
  animation:ring 2s infinite;
}
.call-ring:nth-child(2){animation-delay:.6s}
.call-ring:nth-child(3){animation-delay:1.2s}
@keyframes ring{0%{transform:scale(1);opacity:.6}100%{transform:scale(1.6);opacity:0}}
.call-name{font-size:22px;font-weight:400;letter-spacing:.04em}
.call-status{font-size:12px;color:var(--text2);font-family:'JetBrains Mono',monospace;letter-spacing:.08em}

/* 波形 */
#waveform{display:flex;align-items:center;gap:3px;height:32px}
.wave-bar{
  width:3px;border-radius:2px;background:var(--accent2);
  animation:wave-idle 1.8s infinite ease-in-out;
  transform-origin:center;
}
.wave-bar:nth-child(1){height:8px;animation-delay:0s}
.wave-bar:nth-child(2){height:14px;animation-delay:.15s}
.wave-bar:nth-child(3){height:20px;animation-delay:.3s}
.wave-bar:nth-child(4){height:14px;animation-delay:.45s}
.wave-bar:nth-child(5){height:8px;animation-delay:.6s}
.wave-bar:nth-child(6){height:14px;animation-delay:.75s}
.wave-bar:nth-child(7){height:20px;animation-delay:.9s}
@keyframes wave-idle{0%,100%{transform:scaleY(1);opacity:.4}50%{transform:scaleY(1.5);opacity:1}}
#waveform.listening .wave-bar{background:var(--call);animation:wave-listen 0.1s infinite}
#waveform.speaking .wave-bar{background:var(--accent2);animation:wave-speak .4s infinite ease-in-out}
@keyframes wave-listen{0%,100%{transform:scaleY(var(--h,1))}50%{transform:scaleY(calc(var(--h,1)*1.3))}}
@keyframes wave-speak{0%,100%{transform:scaleY(1)}50%{transform:scaleY(2)}}

.call-end-btn{
  padding:12px 32px;border-radius:50px;
  background:#2e1010;color:var(--danger);
  border:1px solid #4a1a1a;font-size:13px;
  cursor:pointer;font-family:'Noto Serif TC',serif;
  transition:all .2s;letter-spacing:.04em;
}
.call-end-btn:hover{background:#3e1414}

/* VAD 狀態指示 */
.vad-indicator{
  width:8px;height:8px;border-radius:50%;
  background:var(--text3);transition:background .2s;
}
.vad-indicator.active{background:var(--call);box-shadow:0 0 6px var(--call)}

@media(max-width:680px){
  #sidebar{position:fixed;left:-280px;top:0;height:100%;z-index:100;transition:left .25s}
  #sidebar.open{left:0;box-shadow:4px 0 20px #0009}
  #toggleSB{display:block}
}
#toggleSB{display:none;background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer;margin-right:4px}
</style>
</head>
<body>

<!-- ── 側欄 ── -->
<div id="sidebar">
  <div class="s-sec">
    <div class="s-title">系統狀態</div>
    <div class="stat-row"><span>時間</span><span class="stat-val" id="sTime">--</span></div>
    <div class="stat-row"><span>閒置</span><span class="stat-val" id="sIdle">--</span></div>
    <div class="stat-row"><span>短期記憶</span><span class="stat-val" id="sShort">--</span></div>
    <div class="stat-row"><span>長期記憶</span><span class="stat-val" id="sLong">--</span></div>
    <div class="stat-row"><span>新聞快取</span><span class="stat-val" id="sNews">--</span></div>
  </div>

  <div class="s-sec">
    <div class="s-title">回覆模式</div>
    <button class="mode-btn" data-mode="short"  onclick="setMode('short')">⚡ 省流</button>
    <button class="mode-btn active" data-mode="normal" onclick="setMode('normal')">✨ 標準</button>
    <button class="mode-btn" data-mode="long"   onclick="setMode('long')">📝 深度</button>
  </div>

  <div class="s-sec">
    <div class="s-title">語音通話</div>
    <button id="callBtn" onclick="toggleCall()">
      <span class="call-dot"></span>
      <span id="callBtnText">開始通話</span>
    </button>
  </div>

  <div class="s-sec">
    <div class="s-title">快速動作</div>
    <button class="act-btn btn-purple" onclick="triggerCare()">💗 強制觸發關心</button>
    <button class="act-btn btn-danger"  onclick="doReset()">🗑️ 清除短期記憶</button>
  </div>

  <div class="s-sec" style="flex:1">
    <div class="s-title">Persona 編輯</div>
    <div class="p-tabs">
      <span class="ptab active" data-b="base_identity" onclick="switchBlock('base_identity')">身分</span>
      <span class="ptab" data-b="style_short"   onclick="switchBlock('style_short')">省流</span>
      <span class="ptab" data-b="style_normal"  onclick="switchBlock('style_normal')">標準</span>
      <span class="ptab" data-b="style_long"    onclick="switchBlock('style_long')">深度</span>
      <span class="ptab" data-b="time_rules"    onclick="switchBlock('time_rules')">時段</span>
      <span class="ptab" data-b="absence_rules" onclick="switchBlock('absence_rules')">消失</span>
      <span class="ptab" data-b="news_rules"    onclick="switchBlock('news_rules')">新聞</span>
    </div>
    <textarea class="p-editor" id="pEditor"></textarea>
    <div class="p-hint">修改後點「套用」，下一條訊息生效。</div>
    <div class="p-btns">
      <button id="btnApply" onclick="applyPersona()">套用</button>
      <button id="btnReset" onclick="resetPersona()">重設原始</button>
    </div>
    <div class="p-status" id="pStatus"></div>
  </div>
</div>

<!-- ── 聊天主區 ── -->
<div id="main">
  <header>
    <button id="toggleSB" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
    <div class="avatar">🌙</div>
    <div>
      <div class="h-name">莉莉絲</div>
      <div class="h-sub" id="hSub">連線中…</div>
    </div>
  </header>
  <div id="msgs"></div>
  <footer>
    <textarea id="inp" rows="1" placeholder="說點什麼…" maxlength="2000"></textarea>
    <button id="send">➤</button>
  </footer>

  <!-- 通話覆蓋層 -->
  <div id="callOverlay">
    <div class="call-avatar">
      🌙
      <div class="call-ring"></div>
      <div class="call-ring"></div>
      <div class="call-ring"></div>
    </div>
    <div class="call-name">莉莉絲</div>
    <div class="call-status" id="callStatus">準備中…</div>
    <div id="waveform">
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div>
    </div>
    <button class="call-end-btn" onclick="toggleCall()">結束通話</button>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════
// 狀態
// ═══════════════════════════════════════════════════════════
let curMode  = 'normal';
let curBlock = 'base_identity';
let pData    = {};
let ttsCfg   = null;

// 通話狀態
let isCallActive    = false;
let isListening     = false;
let isSpeaking      = false;
let mediaStream     = null;
let audioContext    = null;
let analyser        = null;
let mediaRecorder   = null;
let audioChunks     = [];
let silenceTimer    = null;
let currentAudio    = null;
let ttsQueue        = [];
let ttsPlaying      = false;
let sentenceBuffer  = "";
let vadActive       = false;
const SILENCE_MS    = 1500;  // 靜音超過此時間視為說完
const VOICE_THRESH  = 15;    // 音量門檻（0-255）

// ═══════════════════════════════════════════════════════════
// 狀態刷新
// ═══════════════════════════════════════════════════════════
async function fetchStatus() {
  try {
    const d = await (await fetch('/status')).json();
    document.getElementById('sTime').textContent  = d.time;
    document.getElementById('sIdle').textContent  = d.minutes_idle + ' 分';
    document.getElementById('sShort').textContent = d.short_term_count + ' 條';
    document.getElementById('sLong').textContent  = d.long_term_count  + ' 條';
    document.getElementById('sNews').textContent  = d.has_news_cache ? '有' : '無';
    document.getElementById('hSub').textContent   =
      d.length_mode + ' 模式・記憶 ' + d.long_term_count + ' 條';
    curMode = d.length_mode;
    document.querySelectorAll('.mode-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === d.length_mode));
  } catch {}
}

async function setMode(m) {
  curMode = m;
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === m));
  await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({length_mode:m})});
}

// ═══════════════════════════════════════════════════════════
// 通話核心
// ═══════════════════════════════════════════════════════════
async function toggleCall() {
  if (!isCallActive) {
    await startCall();
  } else {
    stopCall();
  }
}

async function startCall() {
  try {
    // 取得 TTS 設定
    if (!ttsCfg) {
      ttsCfg = await (await fetch('/tts/config')).json();
    }
    // 麥克風權限
    mediaStream  = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioContext = new AudioContext();
    analyser     = audioContext.createAnalyser();
    analyser.fftSize = 256;
    const src = audioContext.createMediaStreamSource(mediaStream);
    src.connect(analyser);

    isCallActive = true;
    document.getElementById('callOverlay').classList.add('active');
    document.getElementById('callBtn').classList.add('active');
    document.getElementById('callBtnText').textContent = '通話中';
    setCallStatus('聆聽中…');
    setWaveState('listening');
    startVAD();
  } catch(e) {
    alert('無法開啟麥克風：' + e.message);
  }
}

function stopCall() {
  isCallActive = false;
  if (mediaStream) {
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
  if (silenceTimer) clearTimeout(silenceTimer);
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  ttsQueue   = [];
  ttsPlaying = false;
  isListening = false;
  isSpeaking  = false;
  vadActive   = false;

  document.getElementById('callOverlay').classList.remove('active');
  document.getElementById('callBtn').classList.remove('active');
  document.getElementById('callBtnText').textContent = '開始通話';
  setWaveState('idle');
}

// ── VAD（語音活動偵測） ────────────────────────────────────
// 人聲頻率範圍 bin 計算（在 AudioContext 建立後調用）
let voiceBinStart = 0;
let voiceBinEnd   = 0;
let voiceOnsetTimer = null;    // 確認說話持續時間的計時器
const VOICE_ONSET_MS = 300;    // 連續超過門檻 300ms 才算真的開始說話

function calcVoiceBins() {
  // 人聲頻率範圍：500Hz ~ 3000Hz
  const sampleRate = audioContext.sampleRate;
  const binCount   = analyser.frequencyBinCount;
  const binSize    = sampleRate / (binCount * 2);
  voiceBinStart = Math.floor(500  / binSize);
  voiceBinEnd   = Math.floor(3000 / binSize);
}

function getVoiceVol(buf) {
  // 只計算人聲頻率範圍的平均音量
  let sum = 0;
  const count = voiceBinEnd - voiceBinStart;
  for (let i = voiceBinStart; i < voiceBinEnd; i++) {
    sum += buf[i];
  }
  return count > 0 ? sum / count : 0;
}

function startVAD() {
  if (!analyser || !isCallActive) return;
  calcVoiceBins();
  const buf = new Uint8Array(analyser.frequencyBinCount);

  function loop() {
    if (!isCallActive) return;
    analyser.getByteFrequencyData(buf);
    const vol = getVoiceVol(buf);  // 只看人聲頻段

    // 更新波形視覺
    updateWaveBars(buf);

    if (!isSpeaking) {
      if (vol > VOICE_THRESH && !vadActive) {
        // 先計時，確認說話持續 300ms 以上才算
        if (!voiceOnsetTimer) {
          voiceOnsetTimer = setTimeout(() => {
            voiceOnsetTimer = null;
            // 再確認一次音量還在
            analyser.getByteFrequencyData(buf);
            if (getVoiceVol(buf) > VOICE_THRESH) {
              vadActive = true;
              startRecording();
              setCallStatus('聆聽中…');
              setWaveState('listening');
              if (silenceTimer) clearTimeout(silenceTimer);
            }
          }, VOICE_ONSET_MS);
        }
      } else if (vol <= VOICE_THRESH) {
        // 音量低：清除 onset 計時
        if (voiceOnsetTimer) {
          clearTimeout(voiceOnsetTimer);
          voiceOnsetTimer = null;
        }
        if (vadActive) {
          // 靜音開始計時
          if (!silenceTimer) {
            silenceTimer = setTimeout(() => {
              if (vadActive) {
                vadActive = false;
                stopRecordingAndSend();
              }
            }, SILENCE_MS);
          }
        }
      } else if (vol > VOICE_THRESH && vadActive && silenceTimer) {
        // 靜音中斷，繼續說話
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
    }
    requestAnimationFrame(loop);
  }
  loop();
}

function updateWaveBars(buf) {
  const bars = document.querySelectorAll('.wave-bar');
  const step = Math.floor(buf.length / bars.length);
  bars.forEach((bar, i) => {
    const val = buf[i * step] / 255;
    bar.style.setProperty('--h', 0.3 + val * 2.5);
  });
}

// ── 錄音 ──────────────────────────────────────────────────
function startRecording() {
  if (!mediaStream) return;
  audioChunks = [];
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType: 'audio/webm' });
  mediaRecorder.ondataavailable = e => {
    if (e.data.size > 0) audioChunks.push(e.data);
  };
  mediaRecorder.start(100);
}

async function stopRecordingAndSend() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  mediaRecorder.stop();
  await new Promise(r => mediaRecorder.onstop = r);

  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  if (blob.size < 2000) return; // 太短忽略

  setCallStatus('理解中…');
  setWaveState('idle');

  // 用 Web Speech API 做 STT（簡單方案）
  // 或者送給 Whisper endpoint（進階方案）
  // 這裡用 Web Speech API
  const text = await speechToText(blob);
  if (!text || text.trim().length < 1) {
    setCallStatus('聆聽中…');
    setWaveState('listening');
    return;
  }

  // 顯示在聊天裡
  addBubble('user', text, ts());
  addBubble('sys', '（通話模式）');

  // 串流取得回覆 + TTS
  await streamReplyAndSpeak(text);
}

// ── Web Speech API STT ─────────────────────────────────────
function speechToText(blob) {
  return new Promise((resolve) => {
    // 方案A：直接用 SpeechRecognition（不需要轉換 blob）
    // 因為我們已經用 VAD 切好了，這裡直接啟動一次短暫辨識
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      resolve('');
      return;
    }
    const r = new SR();
    r.lang = 'zh-TW';
    r.continuous = false;
    r.interimResults = false;
    r.maxAlternatives = 1;

    // 用 blob 的 URL 播放後辨識（實際上 Web Speech 不能吃 blob）
    // 改成：直接從 mediaStream 再辨識一次
    r.onresult = e => resolve(e.results[0][0].transcript);
    r.onerror  = ()  => resolve('');
    r.onend    = ()  => resolve('');

    // 注意：這裡其實是讓 Web Speech 從麥克風辨識
    // VAD 停下來後，我們啟動辨識，快速捕捉最後說的話
    r.start();
    setTimeout(() => { try { r.stop(); } catch{} }, 2000);
  });
}

// ── 串流回覆 + 即時 TTS ────────────────────────────────────
async function streamReplyAndSpeak(userText) {
  isSpeaking  = true;
  setCallStatus('莉莉絲說話中…');
  setWaveState('speaking');

  sentenceBuffer = "";
  ttsQueue       = [];
  ttsPlaying     = false;

  try {
    const resp = await fetch('/chat/stream', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: userText, length_mode: curMode }),
    });

    const reader = resp.body.getReader();
    const dec    = new TextDecoder();
    let   streamBubble = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const lines = dec.decode(value).split('\n');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const { token } = JSON.parse(data);
          if (!token) continue;

          // 更新串流 bubble
          if (!streamBubble) {
            streamBubble = addStreamBubble();
          }
          appendStreamBubble(streamBubble, token);

          // 累積句子
          sentenceBuffer += token;
          const lastChar = sentenceBuffer.slice(-1);
          if ('。！？…\n'.includes(lastChar) && sentenceBuffer.trim().length > 2) {
            const sentence = sentenceBuffer.trim();
            sentenceBuffer = '';
            queueTTS(sentence);
          }
        } catch {}
      }
    }

    // 剩餘文字也 TTS
    if (sentenceBuffer.trim().length > 1) {
      queueTTS(sentenceBuffer.trim());
      sentenceBuffer = '';
    }
    finalizeStreamBubble(streamBubble);

  } catch(e) {
    logger.error?.('stream error', e);
  }

  // 等 TTS 隊列播完
  await waitTTSQueue();

  isSpeaking = false;
  if (isCallActive) {
    setCallStatus('聆聽中…');
    setWaveState('listening');
  }
}

// ── TTS 隊列 ──────────────────────────────────────────────
function queueTTS(text) {
  ttsQueue.push(text);
  if (!ttsPlaying) playNextTTS();
}

async function playNextTTS() {
  if (ttsQueue.length === 0) {
    ttsPlaying = false;
    return;
  }
  ttsPlaying = true;
  const text = ttsQueue.shift();
  await speakText(text);
  playNextTTS();
}

function waitTTSQueue() {
  return new Promise(resolve => {
    const check = setInterval(() => {
      if (!ttsPlaying && ttsQueue.length === 0) {
        clearInterval(check);
        resolve();
      }
    }, 100);
    setTimeout(() => { clearInterval(check); resolve(); }, 30000);
  });
}

async function speakText(text) {
  if (!ttsCfg) return;
  return new Promise(async (resolve) => {
    try {
      const params = new URLSearchParams({
        text:           text,
        text_lang:      ttsCfg.text_lang,
        ref_audio_path: ttsCfg.ref_audio,
        prompt_text:    ttsCfg.prompt_text,
        prompt_lang:    ttsCfg.prompt_lang,
      });
      const resp = await fetch(`${ttsCfg.url}?${params}`);
      if (!resp.ok) { resolve(); return; }
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      currentAudio = new Audio(url);
      currentAudio.onended = () => {
        URL.revokeObjectURL(url);
        currentAudio = null;
        resolve();
      };
      currentAudio.onerror = () => resolve();
      await currentAudio.play();
    } catch { resolve(); }
  });
}

// ── 波形和狀態工具 ─────────────────────────────────────────
function setCallStatus(msg) {
  document.getElementById('callStatus').textContent = msg;
}
function setWaveState(state) {
  const wf = document.getElementById('waveform');
  wf.className = 'idle listening speaking'.includes(state) ? state : '';
  if (state) wf.classList.add(state);
}

// ═══════════════════════════════════════════════════════════
// 聊天 UI
// ═══════════════════════════════════════════════════════════
const msgsEl = document.getElementById('msgs');
const inp    = document.getElementById('inp');
const sendBtn= document.getElementById('send');

inp.addEventListener('input', () => {
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 100) + 'px';
});
inp.addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
sendBtn.addEventListener('click', sendMsg);

function ts() {
  return new Date().toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'});
}
function wait(ms) { return new Promise(r=>setTimeout(r,ms)); }

function addBubble(role, text, time='') {
  const wrap = document.createElement('div');
  wrap.className = 'mwrap ' + role;
  const b = document.createElement('div');
  b.className = 'bubble ' + role;
  b.innerHTML = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/（(.*?)）/g,'<code>（$1）</code>');
  wrap.appendChild(b);
  if (time) {
    const t = document.createElement('div');
    t.className = 'ts'; t.textContent = time;
    wrap.appendChild(t);
  }
  msgsEl.appendChild(wrap);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return wrap;
}

function addStreamBubble() {
  const wrap = document.createElement('div');
  wrap.className = 'mwrap lilith';
  const b = document.createElement('div');
  b.className = 'bubble lilith streaming-cursor';
  b.dataset.raw = '';
  wrap.appendChild(b);
  msgsEl.appendChild(wrap);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return b;
}

function appendStreamBubble(b, token) {
  if (!b) return;
  b.dataset.raw += token;
  b.innerHTML = b.dataset.raw
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/（(.*?)）/g,'<code>（$1）</code>');
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

function finalizeStreamBubble(b) {
  if (!b) return;
  b.classList.remove('streaming-cursor');
  const wrap = b.parentElement;
  const t = document.createElement('div');
  t.className = 'ts'; t.textContent = ts();
  wrap.appendChild(t);
}

function addTyping() {
  const w = document.createElement('div');
  w.id = 'typing'; w.className = 'typing-wrap';
  w.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  msgsEl.appendChild(w);
  msgsEl.scrollTop = msgsEl.scrollHeight;
}
function removeTyping() {
  const el = document.getElementById('typing');
  if (el) el.remove();
}

// 文字模式發送（不走串流，和 v3.0 一致）
async function sendMsg() {
  const text = inp.value.trim();
  if (!text || sendBtn.disabled) return;
  inp.value = ''; inp.style.height = 'auto';
  sendBtn.disabled = true;
  addBubble('user', text, ts());
  addTyping();
  try {
    const d = await (await fetch('/chat',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:text, length_mode:curMode}),
    })).json();
    removeTyping();
    const now = ts();
    for (const line of d.reply.split('\n').filter(l=>l.trim())) {
      await wait(200);
      addBubble('lilith', line, now);
    }
  } catch {
    removeTyping();
    addBubble('sys','連線失敗，請重新整理。');
  }
  sendBtn.disabled = false;
  inp.focus();
}

// 動作
async function doReset() {
  if (!confirm('確定清除短期記憶？')) return;
  await fetch('/reset',{method:'POST'});
  addBubble('sys','🗑️ 短期記憶已清除');
  fetchStatus();
}
async function triggerCare() {
  addTyping();
  try {
    const d = await (await fetch('/care',{method:'POST'})).json();
    removeTyping();
    const now = ts();
    for (const line of d.reply.split('\n').filter(l=>l.trim())) {
      await wait(200);
      addBubble('lilith', line, now);
    }
  } catch { removeTyping(); }
}

// Persona
async function loadPersona() {
  try {
    pData = await (await fetch('/persona')).json();
    document.getElementById('pEditor').value = pData[curBlock] || '';
  } catch {}
}
function switchBlock(b) {
  pData[curBlock] = document.getElementById('pEditor').value;
  curBlock = b;
  document.querySelectorAll('.ptab').forEach(t =>
    t.classList.toggle('active', t.dataset.b === b));
  document.getElementById('pEditor').value = pData[b] || '';
  document.getElementById('pStatus').textContent = '';
}
async function applyPersona() {
  pData[curBlock] = document.getElementById('pEditor').value;
  try {
    const d = await (await fetch('/persona',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(pData),
    })).json();
    showPStatus('✅ ' + d.message);
  } catch { showPStatus('❌ 套用失敗'); }
}
async function resetPersona() {
  if (!confirm('確定重設為原始 Persona？')) return;
  try {
    await fetch('/persona/reset',{method:'POST'});
    await loadPersona();
    showPStatus('✅ 已重設為原始版本');
  } catch { showPStatus('❌ 重設失敗'); }
}
function showPStatus(msg) {
  const el = document.getElementById('pStatus');
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 3000);
}

async function loadHistory() {
  try {
    const d = await (await fetch('/history')).json();
    for (const m of d.history.slice(-8)) {
      addBubble(m.role==='user'?'user':'lilith', m.content);
    }
  } catch {}
}

// 初始化
fetchStatus();
loadHistory();
loadPersona();
setInterval(fetchStatus, 30000);
inp.focus();
</script>
</body>
</html>"""
import logging
import asyncio
from datetime import datetime
from typing import Optional, AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# GPT-SoVITS 本機地址（瀏覽器直連，後端不需要代理）
SOVITS_URL      = "http://127.0.0.1:9880/tts"
SOVITS_REF_WAV  = "C:/wav_ready/vo_cn_lilith_606.wav"
SOVITS_PROMPT   = "这是负担最小的做法了，你很快就会没事的"

# ----------------------------------------------------------
# 📦 Request 模型
# ----------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None

class StreamRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None

class SettingsRequest(BaseModel):
    length_mode: Optional[str] = None

class PersonaBlock(BaseModel):
    base_identity:  Optional[str] = None
    style_short:    Optional[str] = None
    style_normal:   Optional[str] = None
    style_long:     Optional[str] = None
    time_rules:     Optional[str] = None
    absence_rules:  Optional[str] = None
    news_rules:     Optional[str] = None

# ----------------------------------------------------------
# 🔑 Persona Redis Key
# ----------------------------------------------------------
PERSONA_KEY = "lilith:persona_full_template"

def _load_persona_blocks(redis_client) -> dict:
    if redis_client is None:
        return {}
    try:
        raw = redis_client.get(PERSONA_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _save_persona_blocks(redis_client, blocks: dict):
    if redis_client is None:
        return
    try:
        redis_client.set(PERSONA_KEY, json.dumps(blocks, ensure_ascii=False))
    except Exception as e:
        logger.error(f"[web_ui] persona 儲存失敗: {e}")

def _get_default_blocks() -> dict:
    try:
        from core.persona_config import (
            BASE_IDENTITY, STYLE_SHORT, STYLE_NORMAL, STYLE_LONG,
            TIME_RULES, ABSENCE_RULES, NEWS_RULES,
        )
        return {
            "base_identity": BASE_IDENTITY.strip(),
            "style_short":   STYLE_SHORT.strip(),
            "style_normal":  STYLE_NORMAL.strip(),
            "style_long":    STYLE_LONG.strip(),
            "time_rules":    "\n".join(TIME_RULES.values()).strip(),
            "absence_rules": "\n".join(ABSENCE_RULES.values()).strip(),
            "news_rules":    NEWS_RULES.strip(),
        }
    except Exception:
        return {k: "" for k in [
            "base_identity","style_short","style_normal",
            "style_long","time_rules","absence_rules","news_rules"
        ]}

# ----------------------------------------------------------
# 🏗️ App 工廠
# ----------------------------------------------------------
def create_app(admin_id: int, redis_client, deepseek_key: str) -> FastAPI:
    app = FastAPI(title="Lilith Agent", version="4.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    # ── 聊天（標準） ──────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _html()

    @app.post("/chat")
    async def chat(req: ChatRequest):
        from core.redis_store import load_state, save_state
        from interfaces.telegram_bot import generate_reply

        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        try:
            reply = await generate_reply(
                chat_id=admin_id, redis_client=redis_client,
                deepseek_key=deepseek_key, user_text=req.message,
            )
        except Exception as e:
            logger.error(f"[web_ui] generate_reply 失敗: {e}")
            reply = "（系統忙碌中，請稍後再試）"

        return JSONResponse({
            "reply":       reply,
            "length_mode": state.get("length_mode","normal"),
            "timestamp":   datetime.now().strftime("%H:%M"),
        })

    # ── 串流聊天（通話模式） ──────────────────────────────

    @app.post("/chat/stream")
    async def chat_stream(req: StreamRequest):
        from core.redis_store import load_state, save_state, load_history, save_history
        from core.persona_config import get_persona
        from memory.long_term import ensure_index, recall, save as mem_save
        from agent.brain import think_stream

        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        length_mode = state.get("length_mode", "normal")
        history     = load_history(admin_id, redis_client)
        news_text   = state.get("news_cache", "")

        ensure_index(redis_client)
        long_term_ctx = recall(redis_client, admin_id, query=req.message)

        persona = get_persona(
            length_mode=length_mode, news=news_text,
            redis_client=redis_client,
        )
        if long_term_ctx:
            persona += f"\n\n{long_term_ctx}\n"

        messages = [{"role": "system", "content": persona}] + history
        messages.append({"role": "user", "content": req.message})

        full_reply = []

        async def event_generator() -> AsyncGenerator[str, None]:
            async for token in think_stream(messages, length_mode):
                full_reply.append(token)
                yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

            # 背景更新記憶
            reply_text = "".join(full_reply)
            history.append({"role": "user",      "content": req.message})
            history.append({"role": "assistant",  "content": reply_text})
            trimmed = history[-40:]
            save_history(admin_id, trimmed, redis_client)
            save_state(admin_id, state, redis_client)
            asyncio.create_task(_bg_save(redis_client, admin_id, req.message, reply_text))

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _bg_save(redis_client, chat_id, user_text, reply):
        from memory.long_term import save as mem_save
        try:
            await asyncio.to_thread(mem_save, redis_client, chat_id, "user",      user_text)
            await asyncio.to_thread(mem_save, redis_client, chat_id, "assistant", reply)
        except Exception as e:
            logger.error(f"[web_ui] 背景記憶寫入失敗: {e}")

    # ── TTS 設定端點（讓前端知道參數） ───────────────────

    @app.get("/tts/config")
    async def tts_config():
        return JSONResponse({
            "url":         SOVITS_URL,
            "ref_audio":   SOVITS_REF_WAV,
            "prompt_text": SOVITS_PROMPT,
            "prompt_lang": "zh",
            "text_lang":   "zh",
        })

    # ── 其餘端點（與 v3.0 相同） ─────────────────────────

    @app.get("/history")
    async def history():
        from core.redis_store import load_history
        h = load_history(admin_id, redis_client)
        return JSONResponse({"history": h, "count": len(h)})

    @app.get("/status")
    async def status():
        from core.redis_store import load_state, load_history
        from memory.long_term import count
        state   = load_state(admin_id, redis_client)
        history = load_history(admin_id, redis_client)
        n_long  = await asyncio.to_thread(count, redis_client, admin_id)
        last_ts = state.get("last_user_timestamp", 0)
        minutes = int((time.time() - last_ts) / 60) if last_ts else 0
        return JSONResponse({
            "time":             datetime.now().strftime("%H:%M"),
            "minutes_idle":     minutes,
            "length_mode":      state.get("length_mode","normal"),
            "has_news_cache":   bool(state.get("news_cache")),
            "short_term_count": len(history),
            "long_term_count":  n_long,
        })

    @app.post("/settings")
    async def save_settings(req: SettingsRequest):
        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
            save_state(admin_id, state, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/reset")
    async def reset():
        from core.redis_store import save_history, save_state
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care": False, "length_mode": "normal",
        }, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/care")
    async def trigger_care():
        from interfaces.telegram_bot import generate_reply
        reply = await generate_reply(
            chat_id=admin_id, redis_client=redis_client,
            deepseek_key=deepseek_key,
            user_text="(System: 強制觸發主動關心)",
            timer_trigger=True, minutes_since_last=300,
        )
        return JSONResponse({"ok": True, "reply": reply})

    @app.get("/persona")
    async def get_persona_ep():
        overrides = _load_persona_blocks(redis_client)
        defaults  = _get_default_blocks()
        merged    = {k: overrides.get(k, defaults.get(k, "")) for k in defaults}
        return JSONResponse(merged)

    @app.post("/persona")
    async def save_persona(blocks: PersonaBlock):
        current  = _load_persona_blocks(redis_client)
        incoming = {k: v for k, v in blocks.dict().items() if v is not None}
        current.update(incoming)
        _save_persona_blocks(redis_client, current)
        return JSONResponse({"ok": True, "message": "Persona 已套用，下一條訊息生效"})

    @app.post("/persona/reset")
    async def reset_persona():
        if redis_client:
            try:
                redis_client.delete(PERSONA_KEY)
            except Exception:
                pass
        return JSONResponse({"ok": True, "message": "已重設為原始版本"})

    return app


# ----------------------------------------------------------
# 🎨 HTML（含通話 UI）
# ----------------------------------------------------------
def _html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉絲</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@300;400;500&family=JetBrains+Mono:wght@300;400&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0c0c10;--bg2:#111118;--bg3:#16161f;
  --border:#1f1f2e;--border2:#2a2a3a;
  --text:#ddd8d0;--text2:#888;--text3:#444;
  --accent:#8b7cf8;--accent2:#b09af5;--accent3:#6b5ce7;
  --danger:#e07070;--call:#5bc478;--call-active:#3da558;
}
body{font-family:'Noto Serif TC',serif;background:var(--bg);color:var(--text);height:100dvh;display:flex;overflow:hidden}

/* ── 側欄 ── */
#sidebar{width:280px;min-width:280px;background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}
.s-sec{padding:14px 16px;border-bottom:1px solid var(--border)}
.s-title{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px;font-family:'JetBrains Mono',monospace}
.stat-row{display:flex;justify-content:space-between;font-size:12px;color:var(--text2);padding:3px 0;font-family:'JetBrains Mono',monospace}
.stat-val{color:var(--accent2)}
.mode-btn{width:100%;padding:8px 12px;margin-bottom:4px;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;color:var(--text2);font-size:12px;cursor:pointer;text-align:left;transition:all .15s;font-family:'Noto Serif TC',serif}
.mode-btn:hover{background:#1c1c28}
.mode-btn.active{border-color:var(--accent3);color:var(--text);background:#1a1730}
.act-btn{width:100%;padding:8px 12px;margin-bottom:4px;border-radius:6px;font-size:12px;cursor:pointer;border:none;transition:all .15s;text-align:left;font-family:'Noto Serif TC',serif}
.act-btn:hover{opacity:.8}
.btn-purple{background:#1a1730;color:var(--accent2);border:1px solid var(--border2)}
.btn-danger{background:#1e1010;color:var(--danger);border:1px solid #2e1a1a}

/* 通話按鈕 */
#callBtn{
  width:100%;padding:10px 12px;margin-bottom:4px;
  border-radius:6px;font-size:13px;cursor:pointer;
  border:1px solid #2a3e2a;background:#121e14;color:var(--call);
  font-family:'Noto Serif TC',serif;transition:all .2s;
  display:flex;align-items:center;gap:8px;
}
#callBtn:hover{background:#162018}
#callBtn.active{background:#1a3020;border-color:var(--call-active);color:#7aefa0}
#callBtn .call-dot{
  width:8px;height:8px;border-radius:50%;background:var(--call);
  transition:background .2s;flex-shrink:0;
}
#callBtn.active .call-dot{
  background:#7aefa0;
  animation:pulse-dot 1.5s infinite;
}
@keyframes pulse-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}

/* Persona 編輯 */
.p-tabs{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:8px}
.ptab{padding:3px 8px;border-radius:4px;font-size:11px;background:var(--bg3);border:1px solid var(--border2);color:var(--text3);cursor:pointer;transition:all .15s;font-family:'JetBrains Mono',monospace}
.ptab.active{background:#1a1730;border-color:var(--accent3);color:var(--accent2)}
.p-editor{width:100%;min-height:140px;background:#0e0e18;border:1px solid var(--border2);border-radius:6px;color:#ccc;font-size:11px;font-family:'JetBrains Mono',monospace;padding:10px;resize:vertical;line-height:1.5;outline:none}
.p-editor:focus{border-color:var(--accent3)}
.p-hint{font-size:10px;color:var(--text3);margin:4px 0 7px;font-family:'JetBrains Mono',monospace}
.p-btns{display:flex;gap:5px}
.p-btns button{flex:1;padding:6px;border-radius:5px;font-size:11px;cursor:pointer;border:none;transition:opacity .15s;font-family:'Noto Serif TC',serif}
.p-btns button:hover{opacity:.85}
#btnApply{background:var(--accent3);color:#fff}
#btnReset{background:var(--bg3);color:var(--text2);border:1px solid var(--border2)}
.p-status{font-size:10px;color:var(--accent2);margin-top:5px;min-height:14px;font-family:'JetBrains Mono',monospace}

/* ── 主區 ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
header{padding:12px 18px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,var(--accent3),#c46ef7);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.h-name{font-weight:500;font-size:14px;letter-spacing:.02em}
.h-sub{font-size:11px;color:var(--text3);font-family:'JetBrains Mono',monospace}
#msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.mwrap{display:flex;flex-direction:column}
.mwrap.user{align-items:flex-end}
.mwrap.lilith{align-items:flex-start}
.mwrap.sys{align-items:center}
.bubble{max-width:72%;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word;font-weight:300}
.bubble.user{background:var(--accent3);color:#fff;border-bottom-right-radius:3px}
.bubble.lilith{background:var(--bg3);border:1px solid var(--border2);border-bottom-left-radius:3px}
.bubble.sys{background:transparent;color:var(--text3);font-size:11px;font-family:'JetBrains Mono',monospace}
.bubble code{background:var(--bg2);padding:1px 5px;border-radius:3px;font-size:11px;color:var(--accent2);font-family:'JetBrains Mono',monospace}
.ts{font-size:10px;color:var(--text3);margin-top:3px;font-family:'JetBrains Mono',monospace}
.streaming-cursor::after{content:"▋";animation:blink .7s infinite;color:var(--accent2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.typing-wrap{display:flex;align-items:flex-start}
.typing{background:var(--bg3);border:1px solid var(--border2);border-radius:14px;border-bottom-left-radius:3px;padding:10px 14px;display:flex;gap:4px}
.typing span{width:5px;height:5px;border-radius:50%;background:var(--accent3);animation:bounce 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-4px);opacity:1}}
footer{padding:10px 14px;background:var(--bg2);border-top:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
#inp{flex:1;background:var(--bg3);border:1px solid var(--border2);color:var(--text);padding:9px 13px;border-radius:14px;font-size:14px;resize:none;max-height:100px;outline:none;line-height:1.4;font-family:'Noto Serif TC',serif;font-weight:300}
#inp:focus{border-color:var(--accent3)}
#send{width:36px;height:36px;border-radius:50%;background:var(--accent3);border:none;color:#fff;font-size:14px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
#send:hover{background:var(--accent)}
#send:disabled{background:var(--bg3);cursor:not-allowed;border:1px solid var(--border2)}

/* ── 通話覆蓋層 ── */
#callOverlay{
  position:absolute;inset:0;
  background:rgba(10,10,16,.96);
  backdrop-filter:blur(12px);
  display:none;flex-direction:column;
  align-items:center;justify-content:center;
  gap:28px;z-index:50;
}
#callOverlay.active{display:flex}
.call-avatar{
  width:90px;height:90px;border-radius:50%;
  background:linear-gradient(135deg,var(--accent3),#c46ef7);
  display:flex;align-items:center;justify-content:center;
  font-size:38px;position:relative;
}
.call-ring{
  position:absolute;inset:-10px;border-radius:50%;
  border:2px solid var(--accent3);opacity:0;
  animation:ring 2s infinite;
}
.call-ring:nth-child(2){animation-delay:.6s}
.call-ring:nth-child(3){animation-delay:1.2s}
@keyframes ring{0%{transform:scale(1);opacity:.6}100%{transform:scale(1.6);opacity:0}}
.call-name{font-size:22px;font-weight:400;letter-spacing:.04em}
.call-status{font-size:12px;color:var(--text2);font-family:'JetBrains Mono',monospace;letter-spacing:.08em}

/* 波形 */
#waveform{display:flex;align-items:center;gap:3px;height:32px}
.wave-bar{
  width:3px;border-radius:2px;background:var(--accent2);
  animation:wave-idle 1.8s infinite ease-in-out;
  transform-origin:center;
}
.wave-bar:nth-child(1){height:8px;animation-delay:0s}
.wave-bar:nth-child(2){height:14px;animation-delay:.15s}
.wave-bar:nth-child(3){height:20px;animation-delay:.3s}
.wave-bar:nth-child(4){height:14px;animation-delay:.45s}
.wave-bar:nth-child(5){height:8px;animation-delay:.6s}
.wave-bar:nth-child(6){height:14px;animation-delay:.75s}
.wave-bar:nth-child(7){height:20px;animation-delay:.9s}
@keyframes wave-idle{0%,100%{transform:scaleY(1);opacity:.4}50%{transform:scaleY(1.5);opacity:1}}
#waveform.listening .wave-bar{background:var(--call);animation:wave-listen 0.1s infinite}
#waveform.speaking .wave-bar{background:var(--accent2);animation:wave-speak .4s infinite ease-in-out}
@keyframes wave-listen{0%,100%{transform:scaleY(var(--h,1))}50%{transform:scaleY(calc(var(--h,1)*1.3))}}
@keyframes wave-speak{0%,100%{transform:scaleY(1)}50%{transform:scaleY(2)}}

.call-end-btn{
  padding:12px 32px;border-radius:50px;
  background:#2e1010;color:var(--danger);
  border:1px solid #4a1a1a;font-size:13px;
  cursor:pointer;font-family:'Noto Serif TC',serif;
  transition:all .2s;letter-spacing:.04em;
}
.call-end-btn:hover{background:#3e1414}

/* VAD 狀態指示 */
.vad-indicator{
  width:8px;height:8px;border-radius:50%;
  background:var(--text3);transition:background .2s;
}
.vad-indicator.active{background:var(--call);box-shadow:0 0 6px var(--call)}

@media(max-width:680px){
  #sidebar{position:fixed;left:-280px;top:0;height:100%;z-index:100;transition:left .25s}
  #sidebar.open{left:0;box-shadow:4px 0 20px #0009}
  #toggleSB{display:block}
}
#toggleSB{display:none;background:none;border:none;color:var(--text2);font-size:20px;cursor:pointer;margin-right:4px}
</style>
</head>
<body>

<!-- ── 側欄 ── -->
<div id="sidebar">
  <div class="s-sec">
    <div class="s-title">系統狀態</div>
    <div class="stat-row"><span>時間</span><span class="stat-val" id="sTime">--</span></div>
    <div class="stat-row"><span>閒置</span><span class="stat-val" id="sIdle">--</span></div>
    <div class="stat-row"><span>短期記憶</span><span class="stat-val" id="sShort">--</span></div>
    <div class="stat-row"><span>長期記憶</span><span class="stat-val" id="sLong">--</span></div>
    <div class="stat-row"><span>新聞快取</span><span class="stat-val" id="sNews">--</span></div>
  </div>

  <div class="s-sec">
    <div class="s-title">回覆模式</div>
    <button class="mode-btn" data-mode="short"  onclick="setMode('short')">⚡ 省流</button>
    <button class="mode-btn active" data-mode="normal" onclick="setMode('normal')">✨ 標準</button>
    <button class="mode-btn" data-mode="long"   onclick="setMode('long')">📝 深度</button>
  </div>

  <div class="s-sec">
    <div class="s-title">語音通話</div>
    <button id="callBtn" onclick="toggleCall()">
      <span class="call-dot"></span>
      <span id="callBtnText">開始通話</span>
    </button>
  </div>

  <div class="s-sec">
    <div class="s-title">快速動作</div>
    <button class="act-btn btn-purple" onclick="triggerCare()">💗 強制觸發關心</button>
    <button class="act-btn btn-danger"  onclick="doReset()">🗑️ 清除短期記憶</button>
  </div>

  <div class="s-sec" style="flex:1">
    <div class="s-title">Persona 編輯</div>
    <div class="p-tabs">
      <span class="ptab active" data-b="base_identity" onclick="switchBlock('base_identity')">身分</span>
      <span class="ptab" data-b="style_short"   onclick="switchBlock('style_short')">省流</span>
      <span class="ptab" data-b="style_normal"  onclick="switchBlock('style_normal')">標準</span>
      <span class="ptab" data-b="style_long"    onclick="switchBlock('style_long')">深度</span>
      <span class="ptab" data-b="time_rules"    onclick="switchBlock('time_rules')">時段</span>
      <span class="ptab" data-b="absence_rules" onclick="switchBlock('absence_rules')">消失</span>
      <span class="ptab" data-b="news_rules"    onclick="switchBlock('news_rules')">新聞</span>
    </div>
    <textarea class="p-editor" id="pEditor"></textarea>
    <div class="p-hint">修改後點「套用」，下一條訊息生效。</div>
    <div class="p-btns">
      <button id="btnApply" onclick="applyPersona()">套用</button>
      <button id="btnReset" onclick="resetPersona()">重設原始</button>
    </div>
    <div class="p-status" id="pStatus"></div>
  </div>
</div>

<!-- ── 聊天主區 ── -->
<div id="main">
  <header>
    <button id="toggleSB" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
    <div class="avatar">🌙</div>
    <div>
      <div class="h-name">莉莉絲</div>
      <div class="h-sub" id="hSub">連線中…</div>
    </div>
  </header>
  <div id="msgs"></div>
  <footer>
    <textarea id="inp" rows="1" placeholder="說點什麼…" maxlength="2000"></textarea>
    <button id="send">➤</button>
  </footer>

  <!-- 通話覆蓋層 -->
  <div id="callOverlay">
    <div class="call-avatar">
      🌙
      <div class="call-ring"></div>
      <div class="call-ring"></div>
      <div class="call-ring"></div>
    </div>
    <div class="call-name">莉莉絲</div>
    <div class="call-status" id="callStatus">準備中…</div>
    <div id="waveform">
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div><div class="wave-bar"></div>
      <div class="wave-bar"></div>
    </div>
    <button class="call-end-btn" onclick="toggleCall()">結束通話</button>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════
// 狀態
// ═══════════════════════════════════════════════════════════
let curMode  = 'normal';
let curBlock = 'base_identity';
let pData    = {};
let ttsCfg   = null;

// 通話狀態
let isCallActive    = false;
let isListening     = false;
let isSpeaking      = false;
let mediaStream     = null;
let audioContext    = null;
let analyser        = null;
let mediaRecorder   = null;
let audioChunks     = [];
let silenceTimer    = null;
let currentAudio    = null;
let ttsQueue        = [];
let ttsPlaying      = false;
let sentenceBuffer  = "";
let vadActive       = false;
const SILENCE_MS    = 1500;  // 靜音超過此時間視為說完
const VOICE_THRESH  = 15;    // 音量門檻（0-255）

// ═══════════════════════════════════════════════════════════
// 狀態刷新
// ═══════════════════════════════════════════════════════════
async function fetchStatus() {
  try {
    const d = await (await fetch('/status')).json();
    document.getElementById('sTime').textContent  = d.time;
    document.getElementById('sIdle').textContent  = d.minutes_idle + ' 分';
    document.getElementById('sShort').textContent = d.short_term_count + ' 條';
    document.getElementById('sLong').textContent  = d.long_term_count  + ' 條';
    document.getElementById('sNews').textContent  = d.has_news_cache ? '有' : '無';
    document.getElementById('hSub').textContent   =
      d.length_mode + ' 模式・記憶 ' + d.long_term_count + ' 條';
    curMode = d.length_mode;
    document.querySelectorAll('.mode-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === d.length_mode));
  } catch {}
}

async function setMode(m) {
  curMode = m;
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === m));
  await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({length_mode:m})});
}

// ═══════════════════════════════════════════════════════════
// 通話核心
// ═══════════════════════════════════════════════════════════
async function toggleCall() {
  if (!isCallActive) {
    await startCall();
  } else {
    stopCall();
  }
}

async function startCall() {
  try {
    // 取得 TTS 設定
    if (!ttsCfg) {
      ttsCfg = await (await fetch('/tts/config')).json();
    }
    // 麥克風權限
    mediaStream  = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioContext = new AudioContext();
    analyser     = audioContext.createAnalyser();
    analyser.fftSize = 256;
    const src = audioContext.createMediaStreamSource(mediaStream);
    src.connect(analyser);

    isCallActive = true;
    document.getElementById('callOverlay').classList.add('active');
    document.getElementById('callBtn').classList.add('active');
    document.getElementById('callBtnText').textContent = '通話中';
    setCallStatus('聆聽中…');
    setWaveState('listening');
    startVAD();
  } catch(e) {
    alert('無法開啟麥克風：' + e.message);
  }
}

function stopCall() {
  isCallActive = false;
  if (mediaStream) {
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
  if (silenceTimer) clearTimeout(silenceTimer);
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  ttsQueue   = [];
  ttsPlaying = false;
  isListening = false;
  isSpeaking  = false;
  vadActive   = false;

  document.getElementById('callOverlay').classList.remove('active');
  document.getElementById('callBtn').classList.remove('active');
  document.getElementById('callBtnText').textContent = '開始通話';
  setWaveState('idle');
}

// ── VAD（語音活動偵測） ────────────────────────────────────
// 人聲頻率範圍 bin 計算（在 AudioContext 建立後調用）
let voiceBinStart = 0;
let voiceBinEnd   = 0;
let voiceOnsetTimer = null;    // 確認說話持續時間的計時器
const VOICE_ONSET_MS = 300;    // 連續超過門檻 300ms 才算真的開始說話

function calcVoiceBins() {
  // 人聲頻率範圍：500Hz ~ 3000Hz
  const sampleRate = audioContext.sampleRate;
  const binCount   = analyser.frequencyBinCount;
  const binSize    = sampleRate / (binCount * 2);
  voiceBinStart = Math.floor(500  / binSize);
  voiceBinEnd   = Math.floor(3000 / binSize);
}

function getVoiceVol(buf) {
  // 只計算人聲頻率範圍的平均音量
  let sum = 0;
  const count = voiceBinEnd - voiceBinStart;
  for (let i = voiceBinStart; i < voiceBinEnd; i++) {
    sum += buf[i];
  }
  return count > 0 ? sum / count : 0;
}

function startVAD() {
  if (!analyser || !isCallActive) return;
  calcVoiceBins();
  const buf = new Uint8Array(analyser.frequencyBinCount);

  function loop() {
    if (!isCallActive) return;
    analyser.getByteFrequencyData(buf);
    const vol = getVoiceVol(buf);  // 只看人聲頻段

    // 更新波形視覺
    updateWaveBars(buf);

    if (!isSpeaking) {
      if (vol > VOICE_THRESH && !vadActive) {
        // 先計時，確認說話持續 300ms 以上才算
        if (!voiceOnsetTimer) {
          voiceOnsetTimer = setTimeout(() => {
            voiceOnsetTimer = null;
            // 再確認一次音量還在
            analyser.getByteFrequencyData(buf);
            if (getVoiceVol(buf) > VOICE_THRESH) {
              vadActive = true;
              startRecording();
              setCallStatus('聆聽中…');
              setWaveState('listening');
              if (silenceTimer) clearTimeout(silenceTimer);
            }
          }, VOICE_ONSET_MS);
        }
      } else if (vol <= VOICE_THRESH) {
        // 音量低：清除 onset 計時
        if (voiceOnsetTimer) {
          clearTimeout(voiceOnsetTimer);
          voiceOnsetTimer = null;
        }
        if (vadActive) {
          // 靜音開始計時
          if (!silenceTimer) {
            silenceTimer = setTimeout(() => {
              if (vadActive) {
                vadActive = false;
                stopRecordingAndSend();
              }
            }, SILENCE_MS);
          }
        }
      } else if (vol > VOICE_THRESH && vadActive && silenceTimer) {
        // 靜音中斷，繼續說話
        clearTimeout(silenceTimer);
        silenceTimer = null;
      }
    }
    requestAnimationFrame(loop);
  }
  loop();
}

function updateWaveBars(buf) {
  const bars = document.querySelectorAll('.wave-bar');
  const step = Math.floor(buf.length / bars.length);
  bars.forEach((bar, i) => {
    const val = buf[i * step] / 255;
    bar.style.setProperty('--h', 0.3 + val * 2.5);
  });
}

// ── 錄音 ──────────────────────────────────────────────────
function startRecording() {
  if (!mediaStream) return;
  audioChunks = [];
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType: 'audio/webm' });
  mediaRecorder.ondataavailable = e => {
    if (e.data.size > 0) audioChunks.push(e.data);
  };
  mediaRecorder.start(100);
}

async function stopRecordingAndSend() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  mediaRecorder.stop();
  await new Promise(r => mediaRecorder.onstop = r);

  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  if (blob.size < 2000) return; // 太短忽略

  setCallStatus('理解中…');
  setWaveState('idle');

  // 用 Web Speech API 做 STT（簡單方案）
  // 或者送給 Whisper endpoint（進階方案）
  // 這裡用 Web Speech API
  const text = await speechToText(blob);
  if (!text || text.trim().length < 1) {
    setCallStatus('聆聽中…');
    setWaveState('listening');
    return;
  }

  // 顯示在聊天裡
  addBubble('user', text, ts());
  addBubble('sys', '（通話模式）');

  // 串流取得回覆 + TTS
  await streamReplyAndSpeak(text);
}

// ── Web Speech API STT ─────────────────────────────────────
function speechToText(blob) {
  return new Promise((resolve) => {
    // 方案A：直接用 SpeechRecognition（不需要轉換 blob）
    // 因為我們已經用 VAD 切好了，這裡直接啟動一次短暫辨識
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      resolve('');
      return;
    }
    const r = new SR();
    r.lang = 'zh-TW';
    r.continuous = false;
    r.interimResults = false;
    r.maxAlternatives = 1;

    // 用 blob 的 URL 播放後辨識（實際上 Web Speech 不能吃 blob）
    // 改成：直接從 mediaStream 再辨識一次
    r.onresult = e => resolve(e.results[0][0].transcript);
    r.onerror  = ()  => resolve('');
    r.onend    = ()  => resolve('');

    // 注意：這裡其實是讓 Web Speech 從麥克風辨識
    // VAD 停下來後，我們啟動辨識，快速捕捉最後說的話
    r.start();
    setTimeout(() => { try { r.stop(); } catch{} }, 2000);
  });
}

// ── 串流回覆 + 即時 TTS ────────────────────────────────────
async function streamReplyAndSpeak(userText) {
  isSpeaking  = true;
  setCallStatus('莉莉絲說話中…');
  setWaveState('speaking');

  sentenceBuffer = "";
  ttsQueue       = [];
  ttsPlaying     = false;

  try {
    const resp = await fetch('/chat/stream', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: userText, length_mode: curMode }),
    });

    const reader = resp.body.getReader();
    const dec    = new TextDecoder();
    let   streamBubble = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const lines = dec.decode(value).split('\n');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const { token } = JSON.parse(data);
          if (!token) continue;

          // 更新串流 bubble
          if (!streamBubble) {
            streamBubble = addStreamBubble();
          }
          appendStreamBubble(streamBubble, token);

          // 累積句子
          sentenceBuffer += token;
          const lastChar = sentenceBuffer.slice(-1);
          if ('。！？…\n'.includes(lastChar) && sentenceBuffer.trim().length > 2) {
            const sentence = sentenceBuffer.trim();
            sentenceBuffer = '';
            queueTTS(sentence);
          }
        } catch {}
      }
    }

    // 剩餘文字也 TTS
    if (sentenceBuffer.trim().length > 1) {
      queueTTS(sentenceBuffer.trim());
      sentenceBuffer = '';
    }
    finalizeStreamBubble(streamBubble);

  } catch(e) {
    logger.error?.('stream error', e);
  }

  // 等 TTS 隊列播完
  await waitTTSQueue();

  isSpeaking = false;
  if (isCallActive) {
    setCallStatus('聆聽中…');
    setWaveState('listening');
  }
}

// ── TTS 隊列 ──────────────────────────────────────────────
function queueTTS(text) {
  ttsQueue.push(text);
  if (!ttsPlaying) playNextTTS();
}

async function playNextTTS() {
  if (ttsQueue.length === 0) {
    ttsPlaying = false;
    return;
  }
  ttsPlaying = true;
  const text = ttsQueue.shift();
  await speakText(text);
  playNextTTS();
}

function waitTTSQueue() {
  return new Promise(resolve => {
    const check = setInterval(() => {
      if (!ttsPlaying && ttsQueue.length === 0) {
        clearInterval(check);
        resolve();
      }
    }, 100);
    setTimeout(() => { clearInterval(check); resolve(); }, 30000);
  });
}

async function speakText(text) {
  if (!ttsCfg) return;
  return new Promise(async (resolve) => {
    try {
      const params = new URLSearchParams({
        text:           text,
        text_lang:      ttsCfg.text_lang,
        ref_audio_path: ttsCfg.ref_audio,
        prompt_text:    ttsCfg.prompt_text,
        prompt_lang:    ttsCfg.prompt_lang,
      });
      const resp = await fetch(`${ttsCfg.url}?${params}`);
      if (!resp.ok) { resolve(); return; }
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      currentAudio = new Audio(url);
      currentAudio.onended = () => {
        URL.revokeObjectURL(url);
        currentAudio = null;
        resolve();
      };
      currentAudio.onerror = () => resolve();
      await currentAudio.play();
    } catch { resolve(); }
  });
}

// ── 波形和狀態工具 ─────────────────────────────────────────
function setCallStatus(msg) {
  document.getElementById('callStatus').textContent = msg;
}
function setWaveState(state) {
  const wf = document.getElementById('waveform');
  wf.className = 'idle listening speaking'.includes(state) ? state : '';
  if (state) wf.classList.add(state);
}

// ═══════════════════════════════════════════════════════════
// 聊天 UI
// ═══════════════════════════════════════════════════════════
const msgsEl = document.getElementById('msgs');
const inp    = document.getElementById('inp');
const sendBtn= document.getElementById('send');

inp.addEventListener('input', () => {
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 100) + 'px';
});
inp.addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
sendBtn.addEventListener('click', sendMsg);

function ts() {
  return new Date().toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'});
}
function wait(ms) { return new Promise(r=>setTimeout(r,ms)); }

function addBubble(role, text, time='') {
  const wrap = document.createElement('div');
  wrap.className = 'mwrap ' + role;
  const b = document.createElement('div');
  b.className = 'bubble ' + role;
  b.innerHTML = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/（(.*?)）/g,'<code>（$1）</code>');
  wrap.appendChild(b);
  if (time) {
    const t = document.createElement('div');
    t.className = 'ts'; t.textContent = time;
    wrap.appendChild(t);
  }
  msgsEl.appendChild(wrap);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return wrap;
}

function addStreamBubble() {
  const wrap = document.createElement('div');
  wrap.className = 'mwrap lilith';
  const b = document.createElement('div');
  b.className = 'bubble lilith streaming-cursor';
  b.dataset.raw = '';
  wrap.appendChild(b);
  msgsEl.appendChild(wrap);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  return b;
}

function appendStreamBubble(b, token) {
  if (!b) return;
  b.dataset.raw += token;
  b.innerHTML = b.dataset.raw
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/（(.*?)）/g,'<code>（$1）</code>');
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

function finalizeStreamBubble(b) {
  if (!b) return;
  b.classList.remove('streaming-cursor');
  const wrap = b.parentElement;
  const t = document.createElement('div');
  t.className = 'ts'; t.textContent = ts();
  wrap.appendChild(t);
}

function addTyping() {
  const w = document.createElement('div');
  w.id = 'typing'; w.className = 'typing-wrap';
  w.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  msgsEl.appendChild(w);
  msgsEl.scrollTop = msgsEl.scrollHeight;
}
function removeTyping() {
  const el = document.getElementById('typing');
  if (el) el.remove();
}

// 文字模式發送（不走串流，和 v3.0 一致）
async function sendMsg() {
  const text = inp.value.trim();
  if (!text || sendBtn.disabled) return;
  inp.value = ''; inp.style.height = 'auto';
  sendBtn.disabled = true;
  addBubble('user', text, ts());
  addTyping();
  try {
    const d = await (await fetch('/chat',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:text, length_mode:curMode}),
    })).json();
    removeTyping();
    const now = ts();
    for (const line of d.reply.split('\n').filter(l=>l.trim())) {
      await wait(200);
      addBubble('lilith', line, now);
    }
  } catch {
    removeTyping();
    addBubble('sys','連線失敗，請重新整理。');
  }
  sendBtn.disabled = false;
  inp.focus();
}

// 動作
async function doReset() {
  if (!confirm('確定清除短期記憶？')) return;
  await fetch('/reset',{method:'POST'});
  addBubble('sys','🗑️ 短期記憶已清除');
  fetchStatus();
}
async function triggerCare() {
  addTyping();
  try {
    const d = await (await fetch('/care',{method:'POST'})).json();
    removeTyping();
    const now = ts();
    for (const line of d.reply.split('\n').filter(l=>l.trim())) {
      await wait(200);
      addBubble('lilith', line, now);
    }
  } catch { removeTyping(); }
}

// Persona
async function loadPersona() {
  try {
    pData = await (await fetch('/persona')).json();
    document.getElementById('pEditor').value = pData[curBlock] || '';
  } catch {}
}
function switchBlock(b) {
  pData[curBlock] = document.getElementById('pEditor').value;
  curBlock = b;
  document.querySelectorAll('.ptab').forEach(t =>
    t.classList.toggle('active', t.dataset.b === b));
  document.getElementById('pEditor').value = pData[b] || '';
  document.getElementById('pStatus').textContent = '';
}
async function applyPersona() {
  pData[curBlock] = document.getElementById('pEditor').value;
  try {
    const d = await (await fetch('/persona',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(pData),
    })).json();
    showPStatus('✅ ' + d.message);
  } catch { showPStatus('❌ 套用失敗'); }
}
async function resetPersona() {
  if (!confirm('確定重設為原始 Persona？')) return;
  try {
    await fetch('/persona/reset',{method:'POST'});
    await loadPersona();
    showPStatus('✅ 已重設為原始版本');
  } catch { showPStatus('❌ 重設失敗'); }
}
function showPStatus(msg) {
  const el = document.getElementById('pStatus');
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 3000);
}

async function loadHistory() {
  try {
    const d = await (await fetch('/history')).json();
    for (const m of d.history.slice(-8)) {
      addBubble(m.role==='user'?'user':'lilith', m.content);
    }
  } catch {}
}

// 初始化
fetchStatus();
loadHistory();
loadPersona();
setInterval(fetchStatus, 30000);
inp.focus();
</script>
</body>
</html>"""
