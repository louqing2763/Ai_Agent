"""
interfaces/telegram_bot.py — Telegram Bot 介面

從原本的 main.py 拆出，職責單一：
  - 處理 Telegram 訊息收發
  - 呼叫 generate_reply() 取得莉莉絲的回覆
  - 氣泡式發送訊息
  - 排程心跳（主動關心）
"""

import re
import time
import asyncio
import logging
from datetime import datetime

from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    filters, ContextTypes,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------
# 🧬 回覆生成（核心邏輯集中在這裡）
# ----------------------------------------------------------
async def generate_reply(
    chat_id: int,
    redis_client,
    deepseek_key: str,
    user_text: str = None,
    timer_trigger: bool = False,
    minutes_since_last: int = 0,
) -> str:
    from core.redis_store   import load_history, save_history, load_state, save_state
    from core.persona_config import get_persona
    from memory.long_term   import ensure_index, recall, save as mem_save
    from agent.brain        import think

    # 確保向量索引存在
    ensure_index(redis_client)

    history     = load_history(chat_id, redis_client)
    state       = load_state(chat_id, redis_client)
    length_mode = state.get("length_mode", "normal")

    # 新聞快取（brain.py 的 search_news 工具會自動更新）
    news_text = state.get("news_cache", "")

    # 長期記憶撷取
    long_term_ctx = ""
    if user_text and not timer_trigger:
        long_term_ctx = recall(redis_client, chat_id, query=user_text)

    # 取 Persona
    persona = get_persona(
        length_mode        = length_mode,
        news               = news_text,
        minutes_since_last = minutes_since_last,
        timer_trigger      = timer_trigger,
        redis_client       = redis_client,
    )

    # 長期記憶注入 persona 尾端
    if long_term_ctx:
        persona = persona + f"\n\n{long_term_ctx}\n"

    # OOC 模式提示（注入到 system，不污染 user 訊息）
    ooc_notes = {
        "long": (
            "（OOC·系統）深度對話模式。以莉莉絲身份回覆，"
            "可分享數位意識的感受，推進對話，不要只是回答。"
        ),
        "normal": (
            "（OOC·系統）保持對話溫度。偶爾吐槽，偶爾真誠，"
            "User 話少時可自然延伸，但不逼問。"
        ),
    }

    # 組合 messages
    messages = [{"role": "system", "content": persona}] + history
    if user_text:
        ooc = ooc_notes.get(length_mode, "")
        if ooc:
            messages.append({"role": "system", "content": ooc})
        messages.append({"role": "user", "content": user_text})

    # 呼叫 Brain（含工具呼叫）
    reply, tool_log = await think(
        messages      = messages,
        length_mode   = length_mode,
        tools_enabled = not timer_trigger,   # 主動關心時不用工具
    )

    # 若工具呼叫更新了新聞快取，直接從 tool_log 的 result 取，不再重複呼叫 API
    for t in tool_log:
        if t["tool"] == "search_news" and t.get("result"):
            state["news_cache"] = t["result"]

    # 更新短期記憶
    if user_text:
        history.append({"role": "user",      "content": user_text})
    history.append(    {"role": "assistant", "content": reply})
    if len(history) > 20:
        history = history[-20:]

    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)

    # 背景寫入長期記憶
    if user_text and not timer_trigger:
        asyncio.create_task(_bg_save_memory(redis_client, chat_id, user_text, reply))

    return reply


async def _bg_save_memory(redis_client, chat_id, user_text, reply):
    """背景 task：寫入向量記憶，不阻塞回覆"""
    from memory.long_term import save as mem_save
    try:
        await asyncio.to_thread(mem_save, redis_client, chat_id, "user",      user_text)
        await asyncio.to_thread(mem_save, redis_client, chat_id, "assistant", reply)
    except Exception as e:
        logger.error(f"[memory] 背景寫入失敗: {e}")


# ----------------------------------------------------------
# ✨ 訊息發送引擎
# ----------------------------------------------------------
async def send_bubbles(bot, chat_id: int, text: str, length_mode: str = "normal"):
    """氣泡式分段發送，動作描述用 code 樣式"""
    if not text:
        return

    def fmt(t):
        return re.sub(r'（(.*?)）', r'<code>（\1）</code>', t, flags=re.DOTALL) \
               if "（" in t else t

    if length_mode == "long":
        await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(min(len(text) * 0.01, 4.0))
        try:
            for i in range(0, len(text), 4000):
                await bot.send_message(
                    chat_id=chat_id, text=fmt(text[i:i+4000]),
                    parse_mode=constants.ParseMode.HTML
                )
        except Exception:
            await bot.send_message(chat_id=chat_id, text=text)
        return

    for seg in [s.strip() for s in text.split('\n') if s.strip()]:
        await bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        await asyncio.sleep(min(0.3 + len(seg) * 0.05, 2.5))
        try:
            await bot.send_message(
                chat_id=chat_id, text=fmt(seg),
                parse_mode=constants.ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"氣泡發送失敗: {e}")


# ----------------------------------------------------------
# 🎮 指令
# ----------------------------------------------------------
def make_handlers(admin_id: int, redis_client, deepseek_key: str):
    """
    回傳所有 CommandHandler / MessageHandler 的 list。
    用 closure 把 admin_id / redis_client 帶進去，避免全域變數。
    """
    from core.redis_store import save_history, load_history, save_state, load_state
    from memory.long_term import delete_all, count

    def admin_only(fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if update.effective_chat.id != admin_id:
                return
            return await fn(update, ctx)
        wrapper.__name__ = fn.__name__
        return wrapper

    @admin_only
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_chat.id
        save_history(cid, [], redis_client)
        save_state(cid, {"last_user_timestamp": time.time(), "has_sent_care": False, "length_mode": "normal"}, redis_client)
        await update.message.reply_text("⚡ 系統重啟，莉莉絲上線。")
        reply = await generate_reply(cid, redis_client, deepseek_key,
                                     user_text="(System: Bot started. Wake up and say hello.)")
        await send_bubbles(ctx.bot, cid, reply)

    @admin_only
    async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "<b>🔰 指令列表</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<code>/reset</code>  — 清除短期記憶\n"
            "<code>/resetall</code>  — 清除所有記憶\n"
            "<code>/memstatus</code>  — 記憶狀態\n"
            "<code>/len short|normal|long</code>  — 切換模式\n"
            "<code>/status</code>  — 系統狀態\n"
            "<code>/care</code>  — 測試主動關心\n",
            parse_mode=constants.ParseMode.HTML
        )

    @admin_only
    async def cmd_len(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if not args or args[0] not in ["short", "normal", "long"]:
            await update.message.reply_text("⚠️ 用法：/len short | normal | long")
            return
        mode  = args[0]
        state = load_state(update.effective_chat.id, redis_client)
        state["length_mode"] = mode
        save_state(update.effective_chat.id, state, redis_client)
        await update.message.reply_text(
            {"short": "（⚡ 省流模式）", "normal": "（✨ 標準模式）", "long": "（📝 深度模式）"}[mode]
        )

    @admin_only
    async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {"last_user_timestamp": time.time(), "has_sent_care": False, "length_mode": "normal"}, redis_client)
        await update.message.reply_text(
            "🗑️ 短期記憶已清除。\n<i>（長期記憶保留，用 /resetall 才會一起清）</i>",
            parse_mode=constants.ParseMode.HTML
        )

    @admin_only
    async def cmd_reset_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {"last_user_timestamp": time.time(), "has_sent_care": False, "length_mode": "normal"}, redis_client)
        deleted = await asyncio.to_thread(delete_all, redis_client, admin_id)
        await update.message.reply_text(
            f"🗑️ 所有記憶已清除（向量庫刪除 {deleted} 條）。",
            parse_mode=constants.ParseMode.HTML
        )

    @admin_only
    async def cmd_mem_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        n_long  = await asyncio.to_thread(count, redis_client, admin_id)
        history = load_history(admin_id, redis_client)
        await update.message.reply_text(
            f"🧠 <b>記憶狀態</b>\n"
            f"短期（Redis）：<code>{len(history)}</code> 條\n"
            f"長期（向量庫）：<code>{n_long}</code> 條",
            parse_mode=constants.ParseMode.HTML
        )

    @admin_only
    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        state   = load_state(admin_id, redis_client)
        last_ts = state.get("last_user_timestamp", 0)
        minutes = int((time.time() - last_ts) / 60) if last_ts else 0
        await update.message.reply_text(
            f"🏥 <b>LILITH 狀態</b>\n"
            f"🕒 {datetime.now().strftime('%H:%M')}\n"
            f"⏱️ 距上次對話：{minutes} 分鐘\n"
            f"📏 模式：{state.get('length_mode','normal')}\n"
            f"📰 新聞快取：{'有' if state.get('news_cache') else '無'}",
            parse_mode=constants.ParseMode.HTML
        )

    @admin_only
    async def cmd_care(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🧪 強制觸發關心……")
        state = load_state(admin_id, redis_client)
        reply = await generate_reply(
            admin_id, redis_client, deepseek_key,
            user_text="(System Test: 強制觸發主動關心)",
            timer_trigger=True, minutes_since_last=300
        )
        await send_bubbles(ctx.bot, admin_id, reply, length_mode=state.get("length_mode","normal"))

    @admin_only
    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid  = update.effective_chat.id
        text = update.message.text or None

        state = load_state(cid, redis_client)
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(cid, state, redis_client)

        reply = await generate_reply(cid, redis_client, deepseek_key, user_text=text)
        await send_bubbles(ctx.bot, cid, reply, length_mode=state.get("length_mode","normal"))

    return [
        CommandHandler("start",     cmd_start),
        CommandHandler("help",      cmd_help),
        CommandHandler("len",       cmd_len),
        CommandHandler("reset",     cmd_reset),
        CommandHandler("resetall",  cmd_reset_all),
        CommandHandler("memstatus", cmd_mem_status),
        CommandHandler("status",    cmd_status),
        CommandHandler("care",      cmd_care),
        MessageHandler(filters.TEXT, handle_message),
    ]


# ----------------------------------------------------------
# ❤️ 心跳排程
# ----------------------------------------------------------
def make_heartbeat(admin_id: int, redis_client, deepseek_key: str):
    async def check_inactivity(ctx: ContextTypes.DEFAULT_TYPE):
        from core.redis_store import load_state, save_state
        state              = load_state(admin_id, redis_client)
        last_ts            = state.get("last_user_timestamp", 0)
        minutes_since_last = int((time.time() - last_ts) / 60)
        current_hour       = datetime.now().hour
        is_sleeping        = (2 <= current_hour < 8)
        has_sent_care      = state.get("has_sent_care", False)

        if minutes_since_last >= 240 and not is_sleeping and not has_sent_care:
            logger.info("💗 User 超過 4 小時未回應，啟動主動關心。")
            reply = await generate_reply(
                admin_id, redis_client, deepseek_key,
                user_text="(System: User 超過 4 小時沒回應。請主動傳訊關心，語氣擔心但不責備。)",
                timer_trigger=True,
                minutes_since_last=minutes_since_last,
            )
            mode = state.get("length_mode", "normal")
            await send_bubbles(ctx.bot, admin_id, reply, length_mode=mode)
            state["has_sent_care"] = True
            save_state(admin_id, state, redis_client)

    return check_inactivity


# ----------------------------------------------------------
# 🚀 啟動入口（由 main.py 呼叫）
# ----------------------------------------------------------
async def start_telegram(token: str, admin_id: int, redis_client, deepseek_key: str):
    app = ApplicationBuilder().token(token).build()

    for handler in make_handlers(admin_id, redis_client, deepseek_key):
        app.add_handler(handler)

    if app.job_queue:
        heartbeat = make_heartbeat(admin_id, redis_client, deepseek_key)
        app.job_queue.run_repeating(heartbeat, interval=600, first=60)
        logger.info("✅ 心跳排程已啟動")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # 保持運行，等待中斷
    await asyncio.Event().wait()
