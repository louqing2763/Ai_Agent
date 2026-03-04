"""
interfaces/web_ui.py — FastAPI Web UI

提供：
  GET  /          → 聊天介面（單頁 HTML）
  POST /chat      → 發送訊息，取得回覆
  GET  /history   → 取得對話歷史
  GET  /status    → 系統狀態
  POST /reset     → 清除短期記憶
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
# 📦 Request / Response 模型
# ----------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    length_mode: Optional[str] = None   # 若不傳則沿用當前設定


class ChatResponse(BaseModel):
    reply:       str
    tool_calls:  list = []
    length_mode: str  = "normal"
    timestamp:   str  = ""


# ----------------------------------------------------------
# 🏗️ App 工廠
# ----------------------------------------------------------
def create_app(admin_id: int, redis_client, deepseek_key: str) -> FastAPI:
    app = FastAPI(title="Lilith Agent", version="2.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------ 路由 ------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """聊天介面"""
        return _chat_html()

    @app.post("/chat")
    async def chat(req: ChatRequest):
        from core.redis_store import load_state, save_state
        from interfaces.telegram_bot import generate_reply

        state = load_state(admin_id, redis_client)

        # 若有傳入 length_mode，更新設定
        if req.length_mode and req.length_mode in ["short", "normal", "long"]:
            state["length_mode"] = req.length_mode
            save_state(admin_id, state, redis_client)

        # 更新活躍時間
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

        return ChatResponse(
            reply       = reply,
            length_mode = state.get("length_mode", "normal"),
            timestamp   = datetime.now().strftime("%H:%M"),
        )

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
            "time":            datetime.now().strftime("%H:%M"),
            "minutes_idle":    minutes,
            "length_mode":     state.get("length_mode", "normal"),
            "has_news_cache":  bool(state.get("news_cache")),
            "short_term_count": len(history),
            "long_term_count":  n_long,
        })

    @app.post("/reset")
    async def reset():
        from core.redis_store import save_history, save_state
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care":       False,
            "length_mode":         "normal",
        }, redis_client)
        return JSONResponse({"ok": True, "message": "短期記憶已清除"})

    return app


# ----------------------------------------------------------
# 🎨 聊天介面 HTML（單頁，不依賴外部 CDN 以外的資源）
# ----------------------------------------------------------
def _chat_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>莉莉絲</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f13;
      color: #e8e6e3;
      height: 100dvh;
      display: flex;
      flex-direction: column;
    }

    /* Header */
    header {
      padding: 14px 20px;
      background: #1a1a22;
      border-bottom: 1px solid #2a2a35;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .avatar {
      width: 38px; height: 38px;
      border-radius: 50%;
      background: linear-gradient(135deg, #7c6af7, #c46ef7);
      display: flex; align-items: center; justify-content: center;
      font-size: 18px;
    }
    .name { font-weight: 600; font-size: 16px; }
    .subtitle { font-size: 12px; color: #888; }
    .mode-select {
      margin-left: auto;
      background: #2a2a35;
      border: 1px solid #3a3a48;
      color: #ccc;
      padding: 5px 10px;
      border-radius: 8px;
      font-size: 13px;
      cursor: pointer;
    }

    /* Messages */
    #messages {
      flex: 1;
      overflow-y: auto;
      padding: 20px 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .bubble {
      max-width: 75%;
      padding: 10px 14px;
      border-radius: 18px;
      font-size: 15px;
      line-height: 1.55;
      word-break: break-word;
      white-space: pre-wrap;
    }
    .bubble.user {
      align-self: flex-end;
      background: #5c4ef7;
      color: #fff;
      border-bottom-right-radius: 5px;
    }
    .bubble.lilith {
      align-self: flex-start;
      background: #1e1e2a;
      color: #e8e6e3;
      border-bottom-left-radius: 5px;
      border: 1px solid #2a2a38;
    }
    .bubble.system {
      align-self: center;
      background: transparent;
      color: #666;
      font-size: 12px;
      border: none;
      padding: 2px 0;
    }
    .bubble code {
      background: #2a2a35;
      padding: 1px 5px;
      border-radius: 4px;
      font-family: monospace;
      font-size: 13px;
      color: #c9a5f7;
    }
    .ts {
      font-size: 11px;
      color: #555;
      margin-top: 3px;
      text-align: right;
    }
    .bubble.lilith .ts { text-align: left; }

    /* Typing indicator */
    .typing { display: flex; gap: 4px; padding: 12px 14px; }
    .typing span {
      width: 7px; height: 7px;
      border-radius: 50%;
      background: #5c4ef7;
      animation: bounce 1.2s infinite;
    }
    .typing span:nth-child(2) { animation-delay: .2s; }
    .typing span:nth-child(3) { animation-delay: .4s; }
    @keyframes bounce {
      0%, 80%, 100% { transform: translateY(0); opacity: .5; }
      40% { transform: translateY(-6px); opacity: 1; }
    }

    /* Input */
    footer {
      padding: 12px 16px;
      background: #1a1a22;
      border-top: 1px solid #2a2a35;
      display: flex;
      gap: 10px;
      align-items: flex-end;
    }
    #input {
      flex: 1;
      background: #2a2a35;
      border: 1px solid #3a3a48;
      color: #e8e6e3;
      padding: 10px 14px;
      border-radius: 20px;
      font-size: 15px;
      resize: none;
      max-height: 120px;
      overflow-y: auto;
      outline: none;
      line-height: 1.4;
    }
    #input:focus { border-color: #5c4ef7; }
    #send {
      width: 42px; height: 42px;
      border-radius: 50%;
      background: #5c4ef7;
      border: none;
      color: #fff;
      font-size: 18px;
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: background .2s;
      flex-shrink: 0;
    }
    #send:hover  { background: #7c6af7; }
    #send:active { background: #4a3de0; }
    #send:disabled { background: #333; cursor: not-allowed; }
  </style>
</head>
<body>

<header>
  <div class="avatar">🌙</div>
  <div>
    <div class="name">莉莉絲</div>
    <div class="subtitle" id="subtitle">連線中…</div>
  </div>
  <select class="mode-select" id="modeSelect">
    <option value="short">⚡ 省流</option>
    <option value="normal" selected>✨ 標準</option>
    <option value="long">📝 深度</option>
  </select>
</header>

<div id="messages"></div>

<footer>
  <textarea id="input" rows="1" placeholder="說點什麼…" maxlength="2000"></textarea>
  <button id="send">➤</button>
</footer>

<script>
const messagesEl = document.getElementById('messages');
const inputEl    = document.getElementById('input');
const sendBtn    = document.getElementById('send');
const modeSelect = document.getElementById('modeSelect');
const subtitleEl = document.getElementById('subtitle');

// 自動調整輸入框高度
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
});

// Enter 送出（Shift+Enter 換行）
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
sendBtn.addEventListener('click', send);

function addBubble(role, text, ts = '') {
  const wrap = document.createElement('div');
  wrap.style.display = 'flex';
  wrap.style.flexDirection = 'column';
  wrap.style.alignItems = role === 'user' ? 'flex-end' : 'flex-start';

  const bub = document.createElement('div');
  bub.className = `bubble ${role}`;
  // 簡單渲染 <code> 標籤
  bub.innerHTML = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                      .replace(/&lt;code&gt;(.*?)&lt;\/code&gt;/gs, '<code>$1</code>');

  wrap.appendChild(bub);
  if (ts) {
    const t = document.createElement('div');
    t.className = 'ts';
    t.textContent = ts;
    wrap.appendChild(t);
  }
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bub;
}

function addTyping() {
  const wrap = document.createElement('div');
  wrap.style.display = 'flex';
  wrap.style.alignItems = 'flex-start';
  wrap.id = 'typing-indicator';
  wrap.innerHTML = '<div class="bubble lilith typing"><span></span><span></span><span></span></div>';
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typing-indicator');
  if (el) el.remove();
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || sendBtn.disabled) return;

  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;

  const now = new Date().toLocaleTimeString('zh-TW', {hour:'2-digit', minute:'2-digit'});
  addBubble('user', text, now);
  addTyping();

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: text, length_mode: modeSelect.value }),
    });
    const data = await res.json();
    removeTyping();

    // 按換行拆成氣泡（模擬 Telegram 的氣泡效果）
    const lines = data.reply.split('\\n').filter(l => l.trim());
    for (const line of lines) {
      await new Promise(r => setTimeout(r, 300));
      addBubble('lilith', line, now);
    }
  } catch (err) {
    removeTyping();
    addBubble('system', '連線失敗，請重新整理。');
  }

  sendBtn.disabled = false;
  inputEl.focus();
}

// 取得系統狀態
async function fetchStatus() {
  try {
    const res  = await fetch('/status');
    const data = await res.json();
    subtitleEl.textContent = `${data.length_mode} 模式 · 記憶 ${data.long_term_count} 條`;
    modeSelect.value = data.length_mode;
  } catch { subtitleEl.textContent = '已連線'; }
}

// 取歷史記錄（只顯示最近 10 條）
async function loadHistory() {
  try {
    const res  = await fetch('/history');
    const data = await res.json();
    const recent = data.history.slice(-10);
    for (const msg of recent) {
      const role = msg.role === 'user' ? 'user' : 'lilith';
      addBubble(role, msg.content);
    }
  } catch {}
}

fetchStatus();
loadHistory();
setInterval(fetchStatus, 30000);
inputEl.focus();
</script>
</body>
</html>"""
