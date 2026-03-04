"""
interfaces/web_ui.py — FastAPI Web UI v2.1

新增：
  - 設定面板（模式切換、記憶狀態、清除記憶、強制關心）
  - Persona 分區塊編輯（身分、性格、信念、禁忌、時段）
  - POST /settings      — 儲存設定
  - GET  /persona       — 取得目前 persona 區塊
  - POST /persona       — 套用 persona 修改
  - POST /persona/reset — 重設為原始版本
"""

import time
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# 📦 Request 模型
# ----------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None

class SettingsRequest(BaseModel):
    length_mode: Optional[str] = None

class PersonaBlock(BaseModel):
    identity:    Optional[str] = None
    personality: Optional[str] = None
    beliefs:     Optional[str] = None
    forbidden:   Optional[str] = None
    time_rules:  Optional[str] = None

# ----------------------------------------------------------
# 🔑 Persona Redis Key
# ----------------------------------------------------------
PERSONA_KEY = "lilith:persona_overrides"

def _load_persona_blocks(redis_client) -> dict:
    import json
    if redis_client is None:
        return {}
    try:
        raw = redis_client.get(PERSONA_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _save_persona_blocks(redis_client, blocks: dict):
    import json
    if redis_client is None:
        return
    try:
        redis_client.set(PERSONA_KEY, json.dumps(blocks, ensure_ascii=False))
    except Exception as e:
        logger.error(f"[web_ui] persona 儲存失敗: {e}")

def _get_default_blocks() -> dict:
    """從 persona_config.py 切割出各區塊預設內容"""
    try:
        from core.persona_config import BASE_IDENTITY
        text = BASE_IDENTITY

        def extract(start, end=None):
            s = text.find(start)
            if s == -1:
                return ""
            s += len(start)
            if end:
                e = text.find(end, s)
                return text[s:e].strip() if e != -1 else text[s:].strip()
            return text[s:].strip()

        return {
            "identity":    extract("## 核心身分 (Core Identity)\n", "---"),
            "personality": extract("## 性格特質 (Personality Traits)\n", "---"),
            "beliefs":     extract("## 核心信念 (Core Beliefs)\n", "---"),
            "forbidden":   extract("## 語言禁忌 (What Lillith Never Does)\n", "---"),
            "time_rules":  "（時段規則在 get_persona() 動態生成，可在此補充額外規則）",
        }
    except Exception:
        return {k: "" for k in ["identity","personality","beliefs","forbidden","time_rules"]}

# ----------------------------------------------------------
# 🏗️ App 工廠
# ----------------------------------------------------------
def create_app(admin_id: int, redis_client, deepseek_key: str) -> FastAPI:
    app = FastAPI(title="Lilith Agent", version="2.1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    # ── 聊天 ──────────────────────────────────────────────

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
                chat_id      = admin_id,
                redis_client = redis_client,
                deepseek_key = deepseek_key,
                user_text    = req.message,
            )
        except Exception as e:
            logger.error(f"[web_ui] generate_reply 失敗: {e}")
            reply = "（系統忙碌中，請稍後再試）"

        return JSONResponse({
            "reply":       reply,
            "length_mode": state.get("length_mode","normal"),
            "timestamp":   datetime.now().strftime("%H:%M"),
        })

    @app.get("/history")
    async def history():
        from core.redis_store import load_history
        h = load_history(admin_id, redis_client)
        return JSONResponse({"history": h, "count": len(h)})

    @app.get("/status")
    async def status():
        from core.redis_store import load_state, load_history
        from memory.long_term import count
        import asyncio

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

    # ── 設定 ──────────────────────────────────────────────

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
            "has_sent_care":       False,
            "length_mode":         "normal",
        }, redis_client)
        return JSONResponse({"ok": True})

    @app.post("/care")
    async def trigger_care():
        from interfaces.telegram_bot import generate_reply
        reply = await generate_reply(
            chat_id            = admin_id,
            redis_client       = redis_client,
            deepseek_key       = deepseek_key,
            user_text          = "(System: 強制觸發主動關心)",
            timer_trigger      = True,
            minutes_since_last = 300,
        )
        return JSONResponse({"ok": True, "reply": reply})

    # ── Persona ───────────────────────────────────────────

    @app.get("/persona")
    async def get_persona():
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
# 🎨 HTML
# ----------------------------------------------------------
def _html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>莉莉絲</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f0f13;color:#e8e6e3;height:100dvh;display:flex;overflow:hidden}

/* ── 側欄 ── */
#sidebar{width:290px;min-width:290px;background:#13131a;border-right:1px solid #2a2a35;display:flex;flex-direction:column;overflow-y:auto}
.s-sec{padding:14px 16px;border-bottom:1px solid #1e1e2a}
.s-title{font-size:11px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}

/* 狀態 */
.stat-row{display:flex;justify-content:space-between;font-size:13px;color:#888;padding:3px 0}
.stat-val{color:#c9a5f7;font-weight:500}

/* 模式按鈕 */
.mode-btn{width:100%;padding:8px 12px;margin-bottom:5px;background:#1e1e2a;border:1px solid #2a2a38;border-radius:8px;color:#bbb;font-size:13px;cursor:pointer;text-align:left;transition:all .15s}
.mode-btn:hover{background:#25253a}
.mode-btn.active{border-color:#5c4ef7;color:#fff;background:#1e1a3a}

/* 動作按鈕 */
.act-btn{width:100%;padding:8px 12px;margin-bottom:5px;border-radius:8px;font-size:13px;cursor:pointer;border:none;transition:opacity .15s;text-align:left}
.act-btn:hover{opacity:.8}
.btn-purple{background:#1e1a3a;color:#c9a5f7}
.btn-danger{background:#2e1a1a;color:#f97777}

/* Persona 分頁 */
.p-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.ptab{padding:4px 9px;border-radius:6px;font-size:12px;background:#1e1e2a;border:1px solid #2a2a38;color:#888;cursor:pointer;transition:all .15s}
.ptab.active{background:#2a2040;border-color:#7c6af7;color:#c9a5f7}
.p-editor{width:100%;min-height:160px;background:#1a1a24;border:1px solid #2a2a38;border-radius:8px;color:#ddd;font-size:12px;font-family:monospace;padding:10px;resize:vertical;line-height:1.5;outline:none}
.p-editor:focus{border-color:#5c4ef7}
.p-hint{font-size:11px;color:#444;margin:5px 0 8px}
.p-btns{display:flex;gap:6px}
.p-btns button{flex:1;padding:7px;border-radius:7px;font-size:12px;cursor:pointer;border:none;transition:opacity .15s}
.p-btns button:hover{opacity:.85}
#btnApply{background:#5c4ef7;color:#fff}
#btnReset{background:#2a2a35;color:#aaa}
.p-status{font-size:11px;color:#7c6af7;margin-top:6px;min-height:14px}

/* ── 聊天主區 ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
header{padding:12px 18px;background:#1a1a22;border-bottom:1px solid #2a2a35;display:flex;align-items:center;gap:10px;flex-shrink:0}
.avatar{width:34px;height:34px;border-radius:50%;background:linear-gradient(135deg,#7c6af7,#c46ef7);display:flex;align-items:center;justify-content:center;font-size:16px}
.h-name{font-weight:600;font-size:14px}
.h-sub{font-size:11px;color:#555}
#msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px}
.mwrap{display:flex;flex-direction:column}
.mwrap.user{align-items:flex-end}
.mwrap.lilith{align-items:flex-start}
.mwrap.sys{align-items:center}
.bubble{max-width:74%;padding:9px 13px;border-radius:16px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.bubble.user{background:#5c4ef7;color:#fff;border-bottom-right-radius:4px}
.bubble.lilith{background:#1e1e2a;border:1px solid #2a2a38;border-bottom-left-radius:4px}
.bubble.sys{background:transparent;color:#444;font-size:12px}
.bubble code{background:#2a2a35;padding:1px 5px;border-radius:4px;font-size:12px;color:#c9a5f7}
.ts{font-size:11px;color:#333;margin-top:3px}
.typing-wrap{display:flex;align-items:flex-start}
.typing{background:#1e1e2a;border:1px solid #2a2a38;border-radius:16px;border-bottom-left-radius:4px;padding:10px 14px;display:flex;gap:4px}
.typing span{width:6px;height:6px;border-radius:50%;background:#5c4ef7;animation:bounce 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s}
.typing span:nth-child(3){animation-delay:.4s}
@keyframes bounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-5px);opacity:1}}
footer{padding:10px 14px;background:#1a1a22;border-top:1px solid #2a2a35;display:flex;gap:8px;align-items:flex-end;flex-shrink:0}
#inp{flex:1;background:#2a2a35;border:1px solid #3a3a48;color:#e8e6e3;padding:9px 13px;border-radius:18px;font-size:14px;resize:none;max-height:100px;outline:none;line-height:1.4}
#inp:focus{border-color:#5c4ef7}
#send{width:38px;height:38px;border-radius:50%;background:#5c4ef7;border:none;color:#fff;font-size:16px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:background .15s}
#send:hover{background:#7c6af7}
#send:disabled{background:#333;cursor:not-allowed}
#toggleSB{display:none;background:none;border:none;color:#777;font-size:20px;cursor:pointer;margin-right:4px}
@media(max-width:680px){
  #sidebar{position:fixed;left:-290px;top:0;height:100%;z-index:100;transition:left .25s}
  #sidebar.open{left:0;box-shadow:4px 0 20px #0009}
  #toggleSB{display:block}
}
</style>
</head>
<body>

<!-- ── 側欄 ── -->
<div id="sidebar">

  <div class="s-sec">
    <div class="s-title">系統狀態</div>
    <div class="stat-row"><span>時間</span>     <span class="stat-val" id="sTime">--</span></div>
    <div class="stat-row"><span>閒置</span>     <span class="stat-val" id="sIdle">--</span></div>
    <div class="stat-row"><span>短期記憶</span> <span class="stat-val" id="sShort">--</span></div>
    <div class="stat-row"><span>長期記憶</span> <span class="stat-val" id="sLong">--</span></div>
    <div class="stat-row"><span>新聞快取</span> <span class="stat-val" id="sNews">--</span></div>
  </div>

  <div class="s-sec">
    <div class="s-title">回覆模式</div>
    <button class="mode-btn" data-mode="short"  onclick="setMode('short')">⚡ 省流</button>
    <button class="mode-btn active" data-mode="normal" onclick="setMode('normal')">✨ 標準</button>
    <button class="mode-btn" data-mode="long"   onclick="setMode('long')">📝 深度</button>
  </div>

  <div class="s-sec">
    <div class="s-title">快速動作</div>
    <button class="act-btn btn-purple" onclick="triggerCare()">💗 強制觸發關心</button>
    <button class="act-btn btn-danger"  onclick="doReset()">🗑️ 清除短期記憶</button>
  </div>

  <div class="s-sec" style="flex:1">
    <div class="s-title">Persona 編輯</div>
    <div class="p-tabs">
      <span class="ptab active" data-b="identity"    onclick="switchBlock('identity')">身分</span>
      <span class="ptab"        data-b="personality" onclick="switchBlock('personality')">性格</span>
      <span class="ptab"        data-b="beliefs"     onclick="switchBlock('beliefs')">信念</span>
      <span class="ptab"        data-b="forbidden"   onclick="switchBlock('forbidden')">禁忌</span>
      <span class="ptab"        data-b="time_rules"  onclick="switchBlock('time_rules')">時段</span>
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
</div>

<script>
let curMode  = 'normal';
let curBlock = 'identity';
let pData    = {};

// ── 狀態刷新 ──────────────────────────────────────────────
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

// ── 模式切換 ──────────────────────────────────────────────
async function setMode(m) {
  curMode = m;
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.mode === m));
  await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({length_mode:m})});
}

// ── 動作 ──────────────────────────────────────────────────
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

// ── Persona ────────────────────────────────────────────────
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

// ── 聊天 ──────────────────────────────────────────────────
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
      await wait(220);
      addBubble('lilith', line, now);
    }
  } catch {
    removeTyping();
    addBubble('sys','連線失敗，請重新整理。');
  }
  sendBtn.disabled = false;
  inp.focus();
}

async function loadHistory() {
  try {
    const d = await (await fetch('/history')).json();
    for (const m of d.history.slice(-8)) {
      addBubble(m.role==='user'?'user':'lilith', m.content);
    }
  } catch {}
}

// ── 初始化 ────────────────────────────────────────────────
fetchStatus();
loadHistory();
loadPersona();
setInterval(fetchStatus, 30000);
inp.focus();
</script>
</body>
</html>"""
