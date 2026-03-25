"""
interfaces/web_ui.py — FastAPI Web UI v5.0

視覺小說風格介面：
  - 主畫面：全螢幕背景 + 立繪區 + 下方對話框 + 打字機效果
  - 歷史紀錄：往上滑或點按鈕展開聊天泡泡介面
  - 設定：右上角齒輪，側滑面板（模式、Persona、狀態、動作）
  - 通話：左下角按鈕，通話覆蓋層
  - 後端 API 與 v4.0 完全相同
"""

import os
import time
import json
import logging
import asyncio
from datetime import datetime
from typing import Optional, AsyncGenerator

import httpx
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

logger = logging.getLogger(__name__)

SOVITS_URL      = os.environ.get("SOVITS_URL", "http://127.0.0.1:9880/tts")
SOVITS_REF_WAV  = os.environ.get("SOVITS_REF_WAV", "/app/wav_ready/vo_cn_lilith_606.wav")
SOVITS_PROMPT   = os.environ.get("SOVITS_PROMPT", "这是负担最小的做法了，你很快就会没事的")

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

WEB_AUTH_TOKEN = os.environ.get("WEB_AUTH_TOKEN", "")
_security = HTTPBearer(auto_error=False)

async def _check_auth(credentials: HTTPAuthorizationCredentials = Depends(_security)):
    """若設定了 WEB_AUTH_TOKEN 環境變數，則要求 Bearer Token 驗證。"""
    if not WEB_AUTH_TOKEN:
        return  # 未設定 token 時不驗證（向下相容）
    if credentials is None or credentials.credentials != WEB_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

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
    # GET / (HTML 頁面) 不需要驗證，其他 API 端點需要
    app = FastAPI(title="Lilith", version="5.1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _html()

    @app.post("/chat", dependencies=[Depends(_check_auth)])
    async def chat(req: ChatRequest):
        from core.redis_store import load_state, save_state
        from interfaces.discord_bot import generate_reply

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

    @app.post("/chat/stream", dependencies=[Depends(_check_auth)])
    async def chat_stream(req: StreamRequest):
        from core.redis_store import load_state, save_state, load_history, save_history
        from core.persona_config import get_persona
        from memory.long_term import ensure_index, recall
        from agent.brain import think_stream

        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        length_mode   = state.get("length_mode", "normal")
        history       = load_history(admin_id, redis_client)
        news_text     = state.get("news_cache", "")
        ensure_index(redis_client)
        long_term_ctx = recall(redis_client, admin_id, query=req.message)
        persona       = get_persona(
            length_mode=length_mode, news=news_text, redis_client=redis_client,
        )
        if long_term_ctx:
            persona += f"\n\n{long_term_ctx}\n"

        messages = [{"role": "system", "content": persona}] + history
        messages.append({"role": "user", "content": req.message})
        full_reply = []

        async def event_generator():
            async for token in think_stream(messages, length_mode):
                full_reply.append(token)
                yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            reply_text = "".join(full_reply)
            history.append({"role": "user",     "content": req.message})
            history.append({"role": "assistant", "content": reply_text})
            save_history(admin_id, history[-40:], redis_client)
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

    @app.get("/tts/config")
    async def tts_config():
        return JSONResponse({
            "url": SOVITS_URL, "ref_audio": SOVITS_REF_WAV,
            "prompt_text": SOVITS_PROMPT, "prompt_lang": "zh", "text_lang": "zh",
        })

    @app.get("/history", dependencies=[Depends(_check_auth)])
    async def history():
        from core.redis_store import load_history
        h = load_history(admin_id, redis_client)
        return JSONResponse({"history": h, "count": len(h)})

    @app.get("/status", dependencies=[Depends(_check_auth)])
    async def status():
        from core.redis_store import load_state, load_history
        from memory.long_term import count
        state   = load_state(admin_id, redis_client)
        history = load_history(admin_id, redis_client)
        n_long  = await asyncio.to_thread(count, redis_client, admin_id)
        last_ts = state.get("last_user_timestamp", 0)
        minutes = int((time.time() - last_ts) / 60) if last_ts else 0
        return JSONResponse({
            "time": datetime.now().strftime("%H:%M"),
            "minutes_idle": minutes,
            "length_mode":  state.get("length_mode","normal"),
            "has_news_cache":   bool(state.get("news_cache")),
            "short_term_count": len(history),
            "long_term_count":  n_long,
        })

    @app.post("/settings", dependencies=[Depends(_check_auth)])
    async def save_settings(req: SettingsRequest):
        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        if req.length_mode in ["short","normal","long"]:
            state["length_mode"] = req.length_mode
            save_state(admin_id, state, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/reset", dependencies=[Depends(_check_auth)])
    async def reset():
        from core.redis_store import save_history, save_state
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care": False, "length_mode": "normal",
        }, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/care", dependencies=[Depends(_check_auth)])
    async def trigger_care():
        from interfaces.discord_bot import generate_reply
        reply = await generate_reply(
            chat_id=admin_id, redis_client=redis_client,
            deepseek_key=deepseek_key,
            user_text="(System: 強制觸發主動關心)",
            timer_trigger=True, minutes_since_last=300,
        )
        return JSONResponse({"ok": True, "reply": reply})

    @app.get("/persona", dependencies=[Depends(_check_auth)])
    async def get_persona_ep():
        overrides = _load_persona_blocks(redis_client)
        defaults  = _get_default_blocks()
        return JSONResponse({k: overrides.get(k, defaults.get(k, "")) for k in defaults})

    @app.post("/persona", dependencies=[Depends(_check_auth)])
    async def save_persona(blocks: PersonaBlock):
        current  = _load_persona_blocks(redis_client)
        incoming = {k: v for k, v in blocks.dict().items() if v is not None}
        current.update(incoming)
        _save_persona_blocks(redis_client, current)
        return JSONResponse({"ok": True, "message": "Persona 已套用"})

    @app.post("/persona/reset", dependencies=[Depends(_check_auth)])
    async def reset_persona():
        if redis_client:
            try:
                redis_client.delete(PERSONA_KEY)
            except Exception:
                pass
        return JSONResponse({"ok": True, "message": "已重設為原始版本"})

    return app


# ----------------------------------------------------------
# 🎨 HTML — 視覺小說風格
# ----------------------------------------------------------
def _html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉絲</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@300;400;500&family=Zen+Kurenaido&family=JetBrains+Mono:wght@300;400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#08080e;
  --glass:rgba(8,8,18,.75);
  --glass2:rgba(12,12,24,.88);
  --border:rgba(180,160,255,.08);
  --border2:rgba(180,160,255,.15);
  --text:#e8e0f0;
  --text2:rgba(220,210,240,.55);
  --text3:rgba(180,170,210,.3);
  --accent:#a78bfa;
  --accent2:#c4b5fd;
  --accent3:#7c3aed;
  --pink:#f0abfc;
  --call:#6ee7b7;
  --danger:#fca5a5;
  --serif:'Noto Serif TC',serif;
  --mono:'JetBrains Mono',monospace;
  --zen:'Zen Kurenaido',serif;
}

html,body{width:100%;height:100%;overflow:hidden;background:var(--bg)}
body{font-family:var(--serif);color:var(--text);position:relative}

/* ══════════════════════════════════════════
   背景層
══════════════════════════════════════════ */
#bg{
  position:fixed;inset:0;z-index:0;
  background:
    radial-gradient(ellipse 80% 60% at 70% 40%, rgba(124,58,237,.12) 0%, transparent 60%),
    radial-gradient(ellipse 60% 80% at 20% 80%, rgba(167,139,250,.07) 0%, transparent 50%),
    radial-gradient(ellipse 100% 100% at 50% 0%, rgba(240,171,252,.04) 0%, transparent 40%),
    #08080e;
}
/* 細粒噪點紋理 */
#bg::after{
  content:'';position:absolute;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
  opacity:.4;pointer-events:none;
}

/* ══════════════════════════════════════════
   主畫面（視覺小說）
══════════════════════════════════════════ */
#vn{position:fixed;inset:0;z-index:10;display:flex;flex-direction:column}

/* 立繪區 */
#stage{
  flex:1;position:relative;
  display:flex;align-items:flex-end;justify-content:center;
  overflow:hidden;
}

/* 立繪佔位（之後換成真實立繪圖片） */
#sprite{
  position:absolute;bottom:0;left:50%;transform:translateX(-50%);
  width:min(380px,70vw);
  display:flex;flex-direction:column;align-items:center;
  pointer-events:none;
  animation:sprite-idle 6s ease-in-out infinite;
}
@keyframes sprite-idle{
  0%,100%{transform:translateX(-50%) translateY(0)}
  50%{transform:translateX(-50%) translateY(-6px)}
}
.sprite-avatar{
  width:160px;height:160px;border-radius:50%;
  background:linear-gradient(135deg,#4c1d95,#7c3aed,#a855f7,#e879f9);
  display:flex;align-items:center;justify-content:center;
  font-size:72px;
  box-shadow:0 0 60px rgba(167,139,250,.25), 0 0 120px rgba(167,139,250,.1);
  position:relative;
}
.sprite-avatar::before{
  content:'';position:absolute;inset:-3px;border-radius:50%;
  background:linear-gradient(135deg,rgba(167,139,250,.4),rgba(240,171,252,.2),transparent);
  animation:avatar-glow 4s ease-in-out infinite;
}
@keyframes avatar-glow{
  0%,100%{opacity:.6;transform:scale(1)}
  50%{opacity:1;transform:scale(1.02)}
}
.sprite-light{
  width:200px;height:80px;
  background:radial-gradient(ellipse,rgba(167,139,250,.15) 0%,transparent 70%);
  margin-top:-20px;
  animation:light-pulse 4s ease-in-out infinite;
}
@keyframes light-pulse{
  0%,100%{opacity:.6;transform:scaleX(1)}
  50%{opacity:1;transform:scaleX(1.15)}
}

/* 頂部細節 */
#topbar{
  position:absolute;top:0;left:0;right:0;
  padding:16px 20px;
  display:flex;justify-content:space-between;align-items:center;
  background:linear-gradient(to bottom,rgba(8,8,14,.6),transparent);
  z-index:20;
}
.tb-name{font-family:var(--zen);font-size:18px;letter-spacing:.12em;color:var(--accent2);text-shadow:0 0 20px rgba(167,139,250,.4)}
.tb-status{font-family:var(--mono);font-size:10px;color:var(--text3);letter-spacing:.08em}
.tb-right{display:flex;align-items:center;gap:10px}
.icon-btn{
  width:36px;height:36px;border-radius:50%;
  background:rgba(255,255,255,.04);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:16px;transition:all .2s;
  backdrop-filter:blur(8px);
}
.icon-btn:hover{background:rgba(167,139,250,.1);border-color:var(--accent)}

/* 通話按鈕（左下角） */
#callFloatBtn{
  position:absolute;bottom:160px;left:20px;z-index:30;
  width:44px;height:44px;border-radius:50%;
  background:rgba(110,231,183,.08);border:1px solid rgba(110,231,183,.2);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:18px;transition:all .2s;
  backdrop-filter:blur(8px);
}
#callFloatBtn:hover{background:rgba(110,231,183,.15)}
#callFloatBtn.active{
  background:rgba(110,231,183,.15);border-color:var(--call);
  box-shadow:0 0 16px rgba(110,231,183,.2);
  animation:call-pulse 2s infinite;
}
@keyframes call-pulse{0%,100%{box-shadow:0 0 16px rgba(110,231,183,.2)}50%{box-shadow:0 0 28px rgba(110,231,183,.35)}}

/* ── 對話框 ── */
#dialogBox{
  flex-shrink:0;
  margin:0 16px 16px;
  background:var(--glass2);
  border:1px solid var(--border2);
  border-radius:16px;
  backdrop-filter:blur(20px);
  padding:18px 20px 14px;
  min-height:110px;
  position:relative;
  box-shadow:0 -4px 40px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.04);
}
#dialogBox::before{
  content:'';position:absolute;inset:0;border-radius:16px;
  background:linear-gradient(135deg,rgba(167,139,250,.03) 0%,transparent 60%);
  pointer-events:none;
}
#speakerName{
  font-family:var(--zen);font-size:13px;letter-spacing:.1em;
  color:var(--accent2);margin-bottom:8px;
  display:flex;align-items:center;gap:8px;
}
#speakerName::after{
  content:'';flex:1;height:1px;
  background:linear-gradient(to right,rgba(167,139,250,.2),transparent);
}
#dialogText{
  font-size:15px;line-height:1.8;color:var(--text);font-weight:300;
  min-height:48px;letter-spacing:.03em;
}
#dialogText.typing::after{
  content:'▋';color:var(--accent2);animation:blink .65s infinite;font-size:12px;
}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

/* 輸入列 */
#inputRow{
  display:flex;gap:8px;align-items:center;
  margin:0 16px 16px;
}
#inp{
  flex:1;background:rgba(255,255,255,.04);
  border:1px solid var(--border2);border-radius:50px;
  color:var(--text);padding:10px 18px;
  font-size:14px;outline:none;font-family:var(--serif);font-weight:300;
  transition:border-color .2s;
  backdrop-filter:blur(8px);
}
#inp:focus{border-color:var(--accent)}
#inp::placeholder{color:var(--text3)}
#sendBtn{
  width:40px;height:40px;border-radius:50%;flex-shrink:0;
  background:var(--accent3);border:none;color:#fff;
  font-size:15px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:all .2s;
}
#sendBtn:hover{background:var(--accent);transform:scale(1.05)}
#sendBtn:disabled{background:rgba(255,255,255,.06);cursor:not-allowed;transform:none}

/* ══════════════════════════════════════════
   歷史紀錄面板（從底部滑入）
══════════════════════════════════════════ */
#historyPanel{
  position:fixed;inset:0;z-index:50;
  background:rgba(4,4,10,.92);
  backdrop-filter:blur(16px);
  display:flex;flex-direction:column;
  transform:translateY(100%);
  transition:transform .35s cubic-bezier(.32,.72,0,1);
}
#historyPanel.open{transform:translateY(0)}
#historyHeader{
  padding:14px 18px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;
}
#historyHeader span{font-family:var(--zen);font-size:14px;letter-spacing:.08em;color:var(--accent2)}
.close-btn{
  width:32px;height:32px;border-radius:50%;
  background:rgba(255,255,255,.04);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:14px;color:var(--text2);transition:all .15s;
}
.close-btn:hover{background:rgba(255,255,255,.08)}
#historyMsgs{
  flex:1;overflow-y:auto;
  padding:16px;
  display:flex;flex-direction:column;gap:10px;
  scrollbar-width:thin;scrollbar-color:rgba(167,139,250,.2) transparent;
}
.hwrap{display:flex;flex-direction:column}
.hwrap.user{align-items:flex-end}
.hwrap.lilith{align-items:flex-start}
.hwrap.sys{align-items:center}
.hbubble{
  max-width:72%;padding:10px 14px;border-radius:14px;
  font-size:14px;line-height:1.65;white-space:pre-wrap;
  word-break:break-word;font-weight:300;
}
.hbubble.user{background:var(--accent3);color:#fff;border-bottom-right-radius:3px}
.hbubble.lilith{background:rgba(255,255,255,.05);border:1px solid var(--border2);border-bottom-left-radius:3px}
.hbubble.sys{background:transparent;color:var(--text3);font-size:11px;font-family:var(--mono)}
.hbubble code{background:rgba(0,0,0,.3);padding:1px 5px;border-radius:3px;font-size:11px;color:var(--accent2);font-family:var(--mono)}
.hts{font-size:10px;color:var(--text3);margin-top:3px;font-family:var(--mono)}
.htyping{background:rgba(255,255,255,.05);border:1px solid var(--border2);border-radius:14px;border-bottom-left-radius:3px;padding:10px 14px;display:inline-flex;gap:4px}
.htyping span{width:5px;height:5px;border-radius:50%;background:var(--accent3);animation:bounce 1.2s infinite}
.htyping span:nth-child(2){animation-delay:.2s}
.htyping span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-4px);opacity:1}}
/* 歷史面板底部輸入（鏡像主輸入） */
#historyInputRow{
  padding:10px 16px 16px;border-top:1px solid var(--border);
  display:flex;gap:8px;flex-shrink:0;
}
#histInp{
  flex:1;background:rgba(255,255,255,.04);border:1px solid var(--border2);
  border-radius:50px;color:var(--text);padding:10px 18px;
  font-size:14px;outline:none;font-family:var(--serif);font-weight:300;
  transition:border-color .2s;
}
#histInp:focus{border-color:var(--accent)}
#histInp::placeholder{color:var(--text3)}
#histSendBtn{
  width:40px;height:40px;border-radius:50%;flex-shrink:0;
  background:var(--accent3);border:none;color:#fff;
  font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:all .2s;
}
#histSendBtn:hover{background:var(--accent);transform:scale(1.05)}
#histSendBtn:disabled{background:rgba(255,255,255,.06);cursor:not-allowed;transform:none}

/* ══════════════════════════════════════════
   設定面板（從右側滑入）
══════════════════════════════════════════ */
#settingsPanel{
  position:fixed;top:0;right:0;bottom:0;
  width:min(320px,92vw);z-index:60;
  background:rgba(6,6,16,.96);
  border-left:1px solid var(--border);
  backdrop-filter:blur(20px);
  display:flex;flex-direction:column;
  transform:translateX(100%);
  transition:transform .3s cubic-bezier(.32,.72,0,1);
  overflow-y:auto;
}
#settingsPanel.open{transform:translateX(0)}
.sp-header{
  padding:16px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;
}
.sp-header span{font-family:var(--zen);font-size:14px;color:var(--accent2);letter-spacing:.08em}
.sp-sec{padding:14px 18px;border-bottom:1px solid var(--border)}
.sp-title{font-size:10px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.12em;margin-bottom:10px;font-family:var(--mono)}
.stat-row{display:flex;justify-content:space-between;font-size:12px;color:var(--text2);padding:3px 0;font-family:var(--mono)}
.stat-val{color:var(--accent2)}
.mode-btn{width:100%;padding:8px 12px;margin-bottom:4px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;color:var(--text2);font-size:12px;cursor:pointer;text-align:left;transition:all .15s;font-family:var(--serif)}
.mode-btn:hover{background:rgba(167,139,250,.06)}
.mode-btn.active{border-color:var(--accent3);color:var(--text);background:rgba(124,58,237,.12)}
.sp-btn{width:100%;padding:8px 12px;margin-bottom:4px;border-radius:6px;font-size:12px;cursor:pointer;border:none;text-align:left;font-family:var(--serif);transition:all .15s}
.sp-btn:hover{opacity:.8}
.btn-purple{background:rgba(124,58,237,.12);color:var(--accent2);border:1px solid rgba(124,58,237,.2)}
.btn-danger{background:rgba(239,68,68,.08);color:var(--danger);border:1px solid rgba(239,68,68,.15)}
.p-tabs{display:flex;gap:3px;flex-wrap:wrap;margin-bottom:8px}
.ptab{padding:3px 8px;border-radius:4px;font-size:11px;background:rgba(255,255,255,.04);border:1px solid var(--border);color:var(--text3);cursor:pointer;transition:all .15s;font-family:var(--mono)}
.ptab.active{background:rgba(124,58,237,.15);border-color:rgba(167,139,250,.3);color:var(--accent2)}
.p-editor{width:100%;min-height:130px;background:rgba(0,0,0,.3);border:1px solid var(--border);border-radius:6px;color:#ccc;font-size:11px;font-family:var(--mono);padding:10px;resize:vertical;line-height:1.5;outline:none}
.p-editor:focus{border-color:rgba(167,139,250,.3)}
.p-hint{font-size:10px;color:var(--text3);margin:4px 0 7px;font-family:var(--mono)}
.p-btns{display:flex;gap:5px}
.p-btns button{flex:1;padding:7px;border-radius:5px;font-size:11px;cursor:pointer;border:none;transition:opacity .15s;font-family:var(--serif)}
.p-btns button:hover{opacity:.85}
#btnApply{background:var(--accent3);color:#fff}
#btnReset{background:rgba(255,255,255,.05);color:var(--text2);border:1px solid var(--border)}
.p-status{font-size:10px;color:var(--accent2);margin-top:5px;min-height:14px;font-family:var(--mono)}

/* 遮罩 */
#overlay{
  position:fixed;inset:0;z-index:55;
  background:rgba(0,0,0,.4);backdrop-filter:blur(2px);
  opacity:0;pointer-events:none;transition:opacity .3s;
}
#overlay.show{opacity:1;pointer-events:all}

/* ══════════════════════════════════════════
   通話覆蓋層
══════════════════════════════════════════ */
#callOverlay{
  position:fixed;inset:0;z-index:80;
  background:rgba(4,4,12,.95);backdrop-filter:blur(20px);
  display:none;flex-direction:column;
  align-items:center;justify-content:center;gap:28px;
}
#callOverlay.show{display:flex}
.call-avatar-wrap{position:relative;display:flex;align-items:center;justify-content:center}
.call-ring{
  position:absolute;border-radius:50%;
  border:1px solid rgba(167,139,250,.3);
  animation:ring-out 2.4s infinite ease-out;
}
.call-ring:nth-child(1){width:130px;height:130px;animation-delay:0s}
.call-ring:nth-child(2){width:160px;height:160px;animation-delay:.6s}
.call-ring:nth-child(3){width:190px;height:190px;animation-delay:1.2s}
@keyframes ring-out{0%{transform:scale(.9);opacity:.6}100%{transform:scale(1);opacity:0}}
.call-avatar{
  width:100px;height:100px;border-radius:50%;position:relative;z-index:2;
  background:linear-gradient(135deg,#4c1d95,#7c3aed,#a855f7,#e879f9);
  display:flex;align-items:center;justify-content:center;font-size:44px;
  box-shadow:0 0 40px rgba(167,139,250,.3);
}
.call-name{font-family:var(--zen);font-size:24px;letter-spacing:.12em;color:var(--accent2)}
.call-status{font-family:var(--mono);font-size:11px;color:var(--text2);letter-spacing:.1em}
#waveform{display:flex;align-items:center;gap:3px;height:36px}
.wave-bar{width:3px;border-radius:2px;background:var(--accent2);transform-origin:center}
.wave-bar:nth-child(1){height:6px;animation:wb 1s 0s infinite ease-in-out}
.wave-bar:nth-child(2){height:11px;animation:wb 1s .1s infinite ease-in-out}
.wave-bar:nth-child(3){height:18px;animation:wb 1s .2s infinite ease-in-out}
.wave-bar:nth-child(4){height:26px;animation:wb 1s .3s infinite ease-in-out}
.wave-bar:nth-child(5){height:18px;animation:wb 1s .4s infinite ease-in-out}
.wave-bar:nth-child(6){height:11px;animation:wb 1s .5s infinite ease-in-out}
.wave-bar:nth-child(7){height:6px;animation:wb 1s .6s infinite ease-in-out}
@keyframes wb{0%,100%{transform:scaleY(1)}50%{transform:scaleY(1.8)}}
.call-end-btn{
  padding:12px 36px;border-radius:50px;
  background:rgba(252,165,165,.08);color:var(--danger);
  border:1px solid rgba(252,165,165,.2);font-size:13px;
  cursor:pointer;font-family:var(--serif);letter-spacing:.06em;
  transition:all .2s;
}
.call-end-btn:hover{background:rgba(252,165,165,.14)}
</style>
</head>
<body>

<div id="bg"></div>

<!-- ══ 主畫面（視覺小說） ══ -->
<div id="vn">

  <!-- 頂部列 -->
  <div id="topbar">
    <div>
      <div class="tb-name">莉莉絲</div>
      <div class="tb-status" id="tbStatus">連線中…</div>
    </div>
    <div class="tb-right">
      <!-- 歷史紀錄按鈕 -->
      <div class="icon-btn" onclick="openHistory()" title="歷史紀錄">💬</div>
      <!-- 齒輪設定 -->
      <div class="icon-btn" onclick="openSettings()" title="設定">⚙️</div>
    </div>
  </div>

  <!-- 立繪區 -->
  <div id="stage">
    <div id="sprite">
      <div class="sprite-avatar">🌙</div>
      <div class="sprite-light"></div>
    </div>
  </div>

  <!-- 通話浮動按鈕 -->
  <div id="callFloatBtn" onclick="toggleCall()" title="語音通話">📞</div>

  <!-- 對話框 -->
  <div id="dialogBox">
    <div id="speakerName">莉莉絲</div>
    <div id="dialogText">……</div>
  </div>

  <!-- 輸入列 -->
  <div id="inputRow">
    <input id="inp" type="text" placeholder="說點什麼…" maxlength="2000" autocomplete="off">
    <button id="sendBtn">➤</button>
  </div>

</div>

<!-- ══ 遮罩 ══ -->
<div id="overlay" onclick="closeAll()"></div>

<!-- ══ 歷史紀錄面板 ══ -->
<div id="historyPanel">
  <div id="historyHeader">
    <span>對話記錄</span>
    <div class="close-btn" onclick="closeHistory()">✕</div>
  </div>
  <div id="historyMsgs"></div>
  <div id="historyInputRow">
    <input id="histInp" type="text" placeholder="說點什麼…" maxlength="2000" autocomplete="off">
    <button id="histSendBtn">➤</button>
  </div>
</div>

<!-- ══ 設定面板 ══ -->
<div id="settingsPanel">
  <div class="sp-header">
    <span>設定</span>
    <div class="close-btn" onclick="closeSettings()">✕</div>
  </div>

  <div class="sp-sec">
    <div class="sp-title">系統狀態</div>
    <div class="stat-row"><span>時間</span><span class="stat-val" id="sTime">--</span></div>
    <div class="stat-row"><span>閒置</span><span class="stat-val" id="sIdle">--</span></div>
    <div class="stat-row"><span>短期記憶</span><span class="stat-val" id="sShort">--</span></div>
    <div class="stat-row"><span>長期記憶</span><span class="stat-val" id="sLong">--</span></div>
    <div class="stat-row"><span>新聞快取</span><span class="stat-val" id="sNews">--</span></div>
  </div>

  <div class="sp-sec">
    <div class="sp-title">回覆模式</div>
    <button class="mode-btn" data-mode="short"  onclick="setMode('short')">⚡ 省流</button>
    <button class="mode-btn active" data-mode="normal" onclick="setMode('normal')">✨ 標準</button>
    <button class="mode-btn" data-mode="long"   onclick="setMode('long')">📝 深度</button>
  </div>

  <div class="sp-sec">
    <div class="sp-title">動作</div>
    <button class="sp-btn btn-purple" onclick="triggerCare()">💗 強制觸發關心</button>
    <button class="sp-btn btn-danger"  onclick="doReset()">🗑️ 清除短期記憶</button>
  </div>

  <div class="sp-sec" style="flex:1">
    <div class="sp-title">Persona 編輯</div>
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

<!-- ══ 通話覆蓋層 ══ -->
<div id="callOverlay">
  <div class="call-avatar-wrap">
    <div class="call-ring"></div>
    <div class="call-ring"></div>
    <div class="call-ring"></div>
    <div class="call-avatar">🌙</div>
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

<script>
// ═══════════════════════════════════════════════════
// 狀態
// ═══════════════════════════════════════════════════
let curMode  = 'normal';
let curBlock = 'base_identity';
let pData    = {};
let ttsCfg   = null;
let isSending = false;

// 通話
let isCallActive = false;
let isSpeaking   = false;
let vadActive    = false;
let mediaStream  = null;
let audioContext = null;
let analyser     = null;
let mediaRecorder   = null;
let audioChunks     = [];
let silenceTimer    = null;
let voiceOnsetTimer = null;
let currentAudio    = null;
let ttsQueue        = [];
let ttsPlaying      = false;
let sentenceBuffer  = "";
let voiceBinStart   = 0;
let voiceBinEnd     = 0;
const SILENCE_MS      = 1500;
const VOICE_THRESH    = 25;
const VOICE_ONSET_MS  = 300;

// 打字機
let typewriterTimer = null;
let typewriterQueue = [];
let typewriterRunning = false;

// ═══════════════════════════════════════════════════
// 狀態刷新
// ═══════════════════════════════════════════════════
async function fetchStatus() {
  try {
    const d = await (await fetch('/status')).json();
    document.getElementById('sTime').textContent  = d.time;
    document.getElementById('sIdle').textContent  = d.minutes_idle + ' 分';
    document.getElementById('sShort').textContent = d.short_term_count + ' 條';
    document.getElementById('sLong').textContent  = d.long_term_count  + ' 條';
    document.getElementById('sNews').textContent  = d.has_news_cache ? '有' : '無';
    document.getElementById('tbStatus').textContent =
      d.length_mode + ' 模式・' + d.long_term_count + ' 條記憶';
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

// ═══════════════════════════════════════════════════
// 面板開關
// ═══════════════════════════════════════════════════
function openHistory()  { document.getElementById('historyPanel').classList.add('open');    document.getElementById('overlay').classList.add('show'); loadHistoryPanel(); }
function closeHistory() { document.getElementById('historyPanel').classList.remove('open'); document.getElementById('overlay').classList.remove('show'); }
function openSettings() { document.getElementById('settingsPanel').classList.add('open');   document.getElementById('overlay').classList.add('show'); }
function closeSettings(){ document.getElementById('settingsPanel').classList.remove('open');document.getElementById('overlay').classList.remove('show'); }
function closeAll()     { closeHistory(); closeSettings(); }

// ═══════════════════════════════════════════════════
// 打字機效果（對話框）
// ═══════════════════════════════════════════════════
function typewriterShow(text) {
  const el = document.getElementById('dialogText');
  el.classList.add('typing');
  el.textContent = '';
  let i = 0;
  if (typewriterTimer) clearInterval(typewriterTimer);
  typewriterTimer = setInterval(() => {
    if (i < text.length) {
      el.textContent += text[i++];
    } else {
      clearInterval(typewriterTimer);
      typewriterTimer = null;
      setTimeout(() => el.classList.remove('typing'), 300);
    }
  }, 28);
}

function setDialogTyping() {
  const el = document.getElementById('dialogText');
  el.classList.add('typing');
  el.textContent = '……';
}

function setDialogText(text) {
  if (typewriterTimer) clearInterval(typewriterTimer);
  typewriterShow(text);
}

// ═══════════════════════════════════════════════════
// 發送訊息（主對話框）
// ═══════════════════════════════════════════════════
const inp     = document.getElementById('inp');
const sendBtn = document.getElementById('sendBtn');

inp.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); sendMsg(); }
});
sendBtn.addEventListener('click', sendMsg);

async function sendMsg(textOverride) {
  const text = textOverride || inp.value.trim();
  if (!text || isSending) return;
  if (!textOverride) { inp.value = ''; }
  isSending = true;
  sendBtn.disabled = true;
  document.getElementById('histSendBtn').disabled = true;

  // 在歷史面板加入 user bubble
  addHistBubble('user', text, ts());
  setDialogTyping();

  try {
    const d = await (await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, length_mode: curMode}),
    })).json();

    // 對話框打字機
    setDialogText(d.reply);
    // 歷史面板加入回覆
    addHistBubble('lilith', d.reply, d.timestamp);

  } catch {
    setDialogText('（連線失敗，請重新整理）');
  }

  isSending = false;
  sendBtn.disabled = false;
  document.getElementById('histSendBtn').disabled = false;
  inp.focus();
}

// 歷史面板也能發訊息
const histInp     = document.getElementById('histInp');
const histSendBtn = document.getElementById('histSendBtn');
histInp.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); sendFromHistory(); }
});
histSendBtn.addEventListener('click', sendFromHistory);

async function sendFromHistory() {
  const text = histInp.value.trim();
  if (!text) return;
  histInp.value = '';
  await sendMsg(text);
}

// ═══════════════════════════════════════════════════
// 歷史面板 bubble
// ═══════════════════════════════════════════════════
const histMsgs = document.getElementById('historyMsgs');

function addHistBubble(role, text, time='') {
  const wrap = document.createElement('div');
  wrap.className = 'hwrap ' + role;
  const b = document.createElement('div');
  b.className = 'hbubble ' + role;
  b.innerHTML = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/（(.*?)）/g,'<code>（$1）</code>');
  wrap.appendChild(b);
  if (time) {
    const t = document.createElement('div');
    t.className = 'hts'; t.textContent = time;
    wrap.appendChild(t);
  }
  histMsgs.appendChild(wrap);
  histMsgs.scrollTop = histMsgs.scrollHeight;
}

function addHistTyping() {
  const w = document.createElement('div');
  w.id = 'htyping'; w.className = 'hwrap lilith';
  w.innerHTML = '<div class="htyping"><span></span><span></span><span></span></div>';
  histMsgs.appendChild(w);
  histMsgs.scrollTop = histMsgs.scrollHeight;
}
function removeHistTyping() {
  const el = document.getElementById('htyping');
  if (el) el.remove();
}

async function loadHistoryPanel() {
  try {
    const d = await (await fetch('/history')).json();
    histMsgs.innerHTML = '';
    for (const m of d.history) {
      addHistBubble(m.role === 'user' ? 'user' : 'lilith', m.content);
    }
  } catch {}
}

// ═══════════════════════════════════════════════════
// 動作
// ═══════════════════════════════════════════════════
async function doReset() {
  if (!confirm('確定清除短期記憶？')) return;
  await fetch('/reset',{method:'POST'});
  addHistBubble('sys','🗑️ 短期記憶已清除');
  fetchStatus();
}

async function triggerCare() {
  setDialogTyping();
  try {
    const d = await (await fetch('/care',{method:'POST'})).json();
    setDialogText(d.reply);
    addHistBubble('lilith', d.reply, ts());
  } catch { setDialogText('（觸發失敗）'); }
}

// ═══════════════════════════════════════════════════
// Persona
// ═══════════════════════════════════════════════════
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
    showPStatus('✅ 已重設');
  } catch { showPStatus('❌ 重設失敗'); }
}
function showPStatus(msg) {
  const el = document.getElementById('pStatus');
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 3000);
}

// ═══════════════════════════════════════════════════
// 通話
// ═══════════════════════════════════════════════════
async function toggleCall() {
  if (!isCallActive) {
    await startCall();
  } else {
    stopCall();
  }
}

async function startCall() {
  try {
    if (!ttsCfg) ttsCfg = await (await fetch('/tts/config')).json();
    mediaStream  = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioContext = new AudioContext();
    analyser     = audioContext.createAnalyser();
    analyser.fftSize = 512;
    audioContext.createMediaStreamSource(mediaStream).connect(analyser);
    calcVoiceBins();

    isCallActive = true;
    document.getElementById('callOverlay').classList.add('show');
    document.getElementById('callFloatBtn').classList.add('active');
    setCallStatus('聆聽中…');
    startVAD();
  } catch(e) {
    alert('無法開啟麥克風：' + e.message);
  }
}

function stopCall() {
  isCallActive = false;
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  if (audioContext) { audioContext.close(); audioContext = null; }
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  if (silenceTimer)    clearTimeout(silenceTimer);
  if (voiceOnsetTimer) clearTimeout(voiceOnsetTimer);
  if (currentAudio)    { currentAudio.pause(); currentAudio = null; }
  ttsQueue = []; ttsPlaying = false;
  isCallActive = isSpeaking = vadActive = false;
  document.getElementById('callOverlay').classList.remove('show');
  document.getElementById('callFloatBtn').classList.remove('active');
}

function calcVoiceBins() {
  const binSize = audioContext.sampleRate / (analyser.frequencyBinCount * 2);
  voiceBinStart = Math.floor(500  / binSize);
  voiceBinEnd   = Math.floor(3000 / binSize);
}

function getVoiceVol(buf) {
  let sum = 0;
  for (let i = voiceBinStart; i < voiceBinEnd; i++) sum += buf[i];
  const count = voiceBinEnd - voiceBinStart;
  return count > 0 ? sum / count : 0;
}

function startVAD() {
  if (!analyser || !isCallActive) return;
  const buf = new Uint8Array(analyser.frequencyBinCount);
  function loop() {
    if (!isCallActive) return;
    analyser.getByteFrequencyData(buf);
    const vol = getVoiceVol(buf);
    updateWaveBars(buf);
    if (!isSpeaking) {
      if (vol > VOICE_THRESH && !vadActive) {
        if (!voiceOnsetTimer) {
          voiceOnsetTimer = setTimeout(() => {
            voiceOnsetTimer = null;
            analyser.getByteFrequencyData(buf);
            if (getVoiceVol(buf) > VOICE_THRESH) {
              vadActive = true;
              startRecording();
              setCallStatus('聆聽中…');
              if (silenceTimer) clearTimeout(silenceTimer);
            }
          }, VOICE_ONSET_MS);
        }
      } else if (vol <= VOICE_THRESH) {
        if (voiceOnsetTimer) { clearTimeout(voiceOnsetTimer); voiceOnsetTimer = null; }
        if (vadActive && !silenceTimer) {
          silenceTimer = setTimeout(() => {
            if (vadActive) { vadActive = false; stopRecordingAndSend(); }
          }, SILENCE_MS);
        }
      } else if (vol > VOICE_THRESH && vadActive && silenceTimer) {
        clearTimeout(silenceTimer); silenceTimer = null;
      }
    }
    requestAnimationFrame(loop);
  }
  loop();
}

function updateWaveBars(buf) {
  document.querySelectorAll('.wave-bar').forEach((bar, i) => {
    const step = Math.floor(buf.length / 7);
    bar.style.setProperty('--h', 0.3 + (buf[i * step] / 255) * 2.5);
  });
}

function startRecording() {
  if (!mediaStream) return;
  audioChunks = [];
  mediaRecorder = new MediaRecorder(mediaStream, { mimeType: 'audio/webm' });
  mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
  mediaRecorder.start(100);
}

async function stopRecordingAndSend() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  mediaRecorder.stop();
  await new Promise(r => mediaRecorder.onstop = r);
  const blob = new Blob(audioChunks, { type: 'audio/webm' });
  if (blob.size < 2000) { setCallStatus('聆聽中…'); return; }
  setCallStatus('理解中…');
  const text = await speechToText();
  if (!text || text.trim().length < 1) { setCallStatus('聆聽中…'); return; }
  addHistBubble('user', text, ts());
  await streamReplyAndSpeak(text);
}

function speechToText() {
  return new Promise(resolve => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { resolve(''); return; }
    const r = new SR();
    r.lang = 'zh-TW'; r.continuous = false; r.interimResults = false;
    r.onresult = e => resolve(e.results[0][0].transcript);
    r.onerror = r.onend = () => resolve('');
    r.start();
    setTimeout(() => { try { r.stop(); } catch{} }, 2500);
  });
}

async function streamReplyAndSpeak(userText) {
  isSpeaking = true;
  setCallStatus('莉莉絲說話中…');
  setDialogTyping();
  sentenceBuffer = ''; ttsQueue = []; ttsPlaying = false;
  let fullText = '';
  let dialogSet = false;

  try {
    const resp = await fetch('/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: userText, length_mode: curMode }),
    });
    const reader = resp.body.getReader();
    const dec    = new TextDecoder();

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const line of dec.decode(value).split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const { token } = JSON.parse(data);
          if (!token) continue;
          fullText += token;
          sentenceBuffer += token;
          if ('。！？…\n'.includes(sentenceBuffer.slice(-1)) && sentenceBuffer.trim().length > 2) {
            queueTTS(sentenceBuffer.trim());
            sentenceBuffer = '';
          }
        } catch {}
      }
    }
    if (sentenceBuffer.trim().length > 1) { queueTTS(sentenceBuffer.trim()); sentenceBuffer = ''; }
    setDialogText(fullText);
    addHistBubble('lilith', fullText, ts());
  } catch {}

  await waitTTSQueue();
  isSpeaking = false;
  if (isCallActive) setCallStatus('聆聽中…');
}

function queueTTS(text) {
  ttsQueue.push(text);
  if (!ttsPlaying) playNextTTS();
}
async function playNextTTS() {
  if (ttsQueue.length === 0) { ttsPlaying = false; return; }
  ttsPlaying = true;
  await speakText(ttsQueue.shift());
  playNextTTS();
}
function waitTTSQueue() {
  return new Promise(resolve => {
    const check = setInterval(() => {
      if (!ttsPlaying && ttsQueue.length === 0) { clearInterval(check); resolve(); }
    }, 100);
    setTimeout(() => { clearInterval(check); resolve(); }, 30000);
  });
}
async function speakText(text) {
  if (!ttsCfg) return;
  return new Promise(async resolve => {
    try {
      const params = new URLSearchParams({
        text, text_lang: ttsCfg.text_lang,
        ref_audio_path: ttsCfg.ref_audio,
        prompt_text: ttsCfg.prompt_text, prompt_lang: ttsCfg.prompt_lang,
      });
      const resp = await fetch(`${ttsCfg.url}?${params}`);
      if (!resp.ok) { resolve(); return; }
      const url = URL.createObjectURL(await resp.blob());
      currentAudio = new Audio(url);
      currentAudio.onended = () => { URL.revokeObjectURL(url); currentAudio = null; resolve(); };
      currentAudio.onerror = () => resolve();
      await currentAudio.play();
    } catch { resolve(); }
  });
}

function setCallStatus(msg) { document.getElementById('callStatus').textContent = msg; }
function ts() { return new Date().toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'}); }

// ═══════════════════════════════════════════════════
// 初始化
// ═══════════════════════════════════════════════════
async function init() {
  await fetchStatus();
  await loadPersona();

  // 載入最後一條莉莉絲的訊息顯示在對話框
  try {
    const d = await (await fetch('/history')).json();
    const last = [...d.history].reverse().find(m => m.role === 'assistant');
    if (last) {
      document.getElementById('dialogText').textContent = last.content;
    } else {
      document.getElementById('dialogText').textContent = '……你來了。';
    }
  } catch {
    document.getElementById('dialogText').textContent = '……';
  }

  setInterval(fetchStatus, 30000);
  inp.focus();
}

init();
</script>
</body>
</html>"""
