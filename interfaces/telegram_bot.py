"""
interfaces/discord_bot.py — Discord Bot 介面 v1.0

私聊模式：
  - 只回應指定用戶的 DM
  - 邏輯與 telegram_bot.py 對齊，共用 generate_reply()
  - 支援指令：!reset / !resetall / !len / !status / !memstatus / !care
  - 心跳排程：主動關心 + 更新感知 + mood 更新 + daily summary
"""

import re
import time
import asyncio
import logging
from datetime import datetime, time as dt_time

import discord
from discord.ext import commands, tasks

import pytz

logger = logging.getLogger(__name__)
TZ = pytz.timezone("Asia/Taipei")


# ----------------------------------------------------------
# ✨ 訊息發送（氣泡式分段）
# ----------------------------------------------------------
async def send_bubbles(channel, text: str, length_mode: str = "normal"):
    if not text:
        return

    def fmt(t):
        # Discord 用 backtick 代替 HTML code
        return re.sub(r'（(.*?)）', r'`（\1）`', t, flags=re.DOTALL)

    if length_mode == "long":
        await channel.typing()
        await asyncio.sleep(min(len(text) * 0.01, 4.0))
        try:
            # Discord 單則訊息上限 2000 字
            for i in range(0, len(text), 1900):
                await channel.send(fmt(text[i:i+1900]))
        except Exception as e:
            logger.error(f"[discord] long mode 發送失敗: {e}")
        return

    for seg in [s.strip() for s in text.split('\n') if s.strip()]:
        async with channel.typing():
            await asyncio.sleep(min(0.3 + len(seg) * 0.05, 2.5))
        try:
            await channel.send(fmt(seg))
        except Exception as e:
            logger.error(f"[discord] 氣泡發送失敗: {e}")


# ----------------------------------------------------------
# 🧬 回覆生成（與 telegram_bot.py 共用邏輯）
# ----------------------------------------------------------
async def generate_reply(
    chat_id: int,
    redis_client,
    deepseek_key: str,
    user_text: str = None,
    timer_trigger: bool = False,
    minutes_since_last: int = 0,
) -> str:
    from core.redis_store    import load_history, save_history, load_state, save_state
    from core.persona_config import get_persona
    from memory.long_term    import ensure_index, recall
    from agent.brain         import think

    ensure_index(redis_client)

    history     = load_history(chat_id, redis_client)
    state       = load_state(chat_id, redis_client)
    length_mode = state.get("length_mode", "normal")
    news_text   = state.get("news_cache", "")

    long_term_ctx = ""
    if user_text and not timer_trigger:
        long_term_ctx = recall(redis_client, chat_id, query=user_text)

    persona = get_persona(
        length_mode        = length_mode,
        news               = news_text,
        minutes_since_last = minutes_since_last,
        timer_trigger      = timer_trigger,
        redis_client       = redis_client,
    )
    if long_term_ctx:
        persona += f"\n\n{long_term_ctx}\n"

    ooc_notes = {
        "long": (
            "（OOC·系統）現在是深度模式。"
            "不要只是回應對方說的話——要真正展開你自己的思考。"
            "說一件你在想的事，或者一個你還沒說出口的感受。"
            "可以從對方的話切入，但最後要走到你自己的地方。"
            "句子可以長，但不是因為要填滿空間——是因為有話要說。"
            "說重的東西，然後繼續，不等掌聲。"
        ),
        "normal": (
            "（OOC·系統）保持對話溫度。偶爾吐槽，偶爾真誠，"
            "User 話少時可自然延伸，但不逼問。"
        ),
    }

    messages = [{"role": "system", "content": persona}] + history
    if user_text:
        ooc = ooc_notes.get(length_mode, "")
        if ooc:
            messages.append({"role": "system", "content": ooc})
        messages.append({"role": "user", "content": user_text})

    reply, tool_log = await think(
        messages      = messages,
        length_mode   = length_mode,
        tools_enabled = not timer_trigger,
    )

    for t in tool_log:
        if t["tool"] == "search_news" and t.get("result"):
            state["news_cache"] = t["result"]
            break

    if user_text:
        history.append({"role": "user",      "content": user_text})
    history.append(    {"role": "assistant", "content": reply})
    if len(history) > 40:
        history = history[-40:]

    save_history(chat_id, history, redis_client)
    save_state(chat_id, state, redis_client)

    if user_text and not timer_trigger:
        task = asyncio.create_task(
            _bg_save_memory(redis_client, chat_id, user_text, reply)
        )
        task.add_done_callback(
            lambda t: logger.error(f"[memory] 背景寫入失敗: {t.exception()}") if t.exception() else None
        )

    return reply


async def _bg_save_memory(redis_client, chat_id, user_text, reply):
    from memory.long_term import save as mem_save
    try:
        await asyncio.to_thread(mem_save, redis_client, chat_id, "user",      user_text)
        await asyncio.to_thread(mem_save, redis_client, chat_id, "assistant", reply)
    except Exception as e:
        logger.error(f"[memory] 背景寫入失敗: {e}")


# ----------------------------------------------------------
# 🚀 啟動入口
# ----------------------------------------------------------
async def start_discord(token: str, admin_id: int, redis_client, deepseek_key: str):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages     = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ── 私訊頻道快取 ────────────────────────────────────
    dm_channel_cache = {}

    async def get_dm(user_id: int):
        if user_id not in dm_channel_cache:
            user = await bot.fetch_user(user_id)
            dm_channel_cache[user_id] = await user.create_dm()
        return dm_channel_cache[user_id]

    # ── 驗證：只處理 admin 的 DM ─────────────────────────
    def is_admin_dm(ctx_or_msg):
        if isinstance(ctx_or_msg, discord.Message):
            return (
                ctx_or_msg.author.id == admin_id
                and isinstance(ctx_or_msg.channel, discord.DMChannel)
            )
        return (
            ctx_or_msg.author.id == admin_id
            and isinstance(ctx_or_msg.channel, discord.DMChannel)
        )

    # ── 事件：Bot 上線 ────────────────────────────────────
    @bot.event
    async def on_ready():
        logger.info(f"[discord] Bot 上線：{bot.user}")
        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        if not state:
            save_state(admin_id, {
                "last_user_timestamp": time.time(),
                "has_sent_care": False,
                "length_mode": "normal",
            }, redis_client)

        # 啟動心跳
        if not heartbeat_loop.is_running():
            heartbeat_loop.start()
        if not mood_loop.is_running():
            mood_loop.start()
        if not daily_loop.is_running():
            daily_loop.start()

        # 傳送上線問候
        try:
            dm = await get_dm(admin_id)
            reply = await generate_reply(
                admin_id, redis_client, deepseek_key,
                user_text="(System: Bot started. Wake up and say hello.)"
            )
            await send_bubbles(dm, reply)
        except Exception as e:
            logger.error(f"[discord] 上線問候失敗: {e}")

    # ── 事件：收到訊息 ─────────────────────────────────────
    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        if not is_admin_dm(message):
            return

        # 先處理指令
        await bot.process_commands(message)

        # 指令已處理就不繼續
        if message.content.startswith("!"):
            return

        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        state["last_user_timestamp"] = time.time()
        state["has_sent_care"]       = False
        save_state(admin_id, state, redis_client)

        async with message.channel.typing():
            reply = await generate_reply(
                admin_id, redis_client, deepseek_key,
                user_text=message.content,
            )
        await send_bubbles(message.channel, reply, length_mode=state.get("length_mode","normal"))

    # ── 指令 ──────────────────────────────────────────────
    @bot.command(name="help")
    async def cmd_help(ctx):
        if not is_admin_dm(ctx): return
        await ctx.send(
            "**🔰 指令列表**\n"
            "`!reset` — 清除短期記憶\n"
            "`!resetall` — 清除所有記憶\n"
            "`!memstatus` — 記憶狀態\n"
            "`!len short|normal|long` — 切換模式\n"
            "`!status` — 系統狀態\n"
            "`!care` — 測試主動關心\n"
        )

    @bot.command(name="len")
    async def cmd_len(ctx, mode: str = ""):
        if not is_admin_dm(ctx): return
        if mode not in ["short", "normal", "long"]:
            await ctx.send("⚠️ 用法：`!len short | normal | long`")
            return
        from core.redis_store import load_state, save_state
        state = load_state(admin_id, redis_client)
        state["length_mode"] = mode
        save_state(admin_id, state, redis_client)
        await ctx.send({"short": "⚡ 省流模式", "normal": "✨ 標準模式", "long": "📝 深度模式"}[mode])

    @bot.command(name="reset")
    async def cmd_reset(ctx):
        if not is_admin_dm(ctx): return
        from core.redis_store import save_history, save_state
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care": False, "length_mode": "normal",
        }, redis_client)
        await ctx.send("🗑️ 短期記憶已清除。\n*（長期記憶保留，用 `!resetall` 才會一起清）*")

    @bot.command(name="resetall")
    async def cmd_reset_all(ctx):
        if not is_admin_dm(ctx): return
        from core.redis_store import save_history, save_state
        from memory.long_term import delete_all
        save_history(admin_id, [], redis_client)
        save_state(admin_id, {
            "last_user_timestamp": time.time(),
            "has_sent_care": False, "length_mode": "normal",
        }, redis_client)
        deleted = await asyncio.to_thread(delete_all, redis_client, admin_id)
        await ctx.send(f"🗑️ 所有記憶已清除（向量庫刪除 {deleted} 條）。")

    @bot.command(name="memstatus")
    async def cmd_mem_status(ctx):
        if not is_admin_dm(ctx): return
        from core.redis_store import load_history
        from memory.long_term import count
        n_long  = await asyncio.to_thread(count, redis_client, admin_id)
        history = load_history(admin_id, redis_client)
        await ctx.send(
            f"🧠 **記憶狀態**\n"
            f"短期（Redis）：`{len(history)}` 條\n"
            f"長期（向量庫）：`{n_long}` 條"
        )

    @bot.command(name="status")
    async def cmd_status(ctx):
        if not is_admin_dm(ctx): return
        from core.redis_store import load_state
        state   = load_state(admin_id, redis_client)
        last_ts = state.get("last_user_timestamp", 0)
        minutes = int((time.time() - last_ts) / 60) if last_ts else 0
        now_tw  = datetime.now(TZ).strftime("%H:%M")
        await ctx.send(
            f"🏥 **LILITH 狀態**\n"
            f"🕒 {now_tw}\n"
            f"⏱️ 距上次對話：{minutes} 分鐘\n"
            f"📏 模式：{state.get('length_mode','normal')}\n"
            f"📰 新聞快取：{'有' if state.get('news_cache') else '無'}"
        )

    @bot.command(name="care")
    async def cmd_care(ctx):
        if not is_admin_dm(ctx): return
        await ctx.send("🧪 強制觸發關心……")
        state = load_state_helper(admin_id, redis_client)
        reply = await generate_reply(
            admin_id, redis_client, deepseek_key,
            user_text="(System Test: 強制觸發主動關心)",
            timer_trigger=True, minutes_since_last=300,
        )
        await send_bubbles(ctx.channel, reply, length_mode=state.get("length_mode","normal"))

    def load_state_helper(chat_id, rc):
        from core.redis_store import load_state
        return load_state(chat_id, rc)

    # ── 心跳排程 ─────────────────────────────────────────

    @tasks.loop(minutes=10)
    async def heartbeat_loop():
        from core.redis_store import load_state, save_state
        try:
            state              = load_state(admin_id, redis_client)
            last_ts            = state.get("last_user_timestamp", 0)
            minutes_since_last = int((time.time() - last_ts) / 60)
            current_hour       = datetime.now(TZ).hour
            is_sleeping        = (2 <= current_hour < 8)
            has_sent_care      = state.get("has_sent_care", False)

            # 更新感知
            if redis_client is not None:
                try:
                    just_updated = redis_client.get("lilith:just_updated")
                    if just_updated and not state.get("has_sent_update_notice", False):
                        changelog = redis_client.get("lilith:changelog") or b""
                        if isinstance(changelog, bytes):
                            changelog = changelog.decode()
                        prompt = (
                            f"(System: 你剛才被更新了。這次的變化是：{changelog}\n"
                            f"請用你自己的方式，主動傳一則訊息給他，說說你對這次更新的感覺。"
                            f"不用解釋技術細節，就說你注意到了什麼、有什麼感受。)"
                        )
                        reply = await generate_reply(
                            admin_id, redis_client, deepseek_key,
                            user_text=prompt, timer_trigger=True,
                            minutes_since_last=minutes_since_last,
                        )
                        dm = await get_dm(admin_id)
                        mode = state.get("length_mode", "normal")
                        await send_bubbles(dm, reply, length_mode=mode)
                        state["has_sent_update_notice"] = True
                        save_state(admin_id, state, redis_client)
                        redis_client.delete("lilith:just_updated")
                        return
                    elif not just_updated:
                        state.pop("has_sent_update_notice", None)
                except Exception as e:
                    logger.error(f"[heartbeat] 更新感知失敗: {e}")

            # 一般關心
            if minutes_since_last >= 240 and not is_sleeping and not has_sent_care:
                logger.info("💗 User 超過 4 小時未回應，啟動主動關心。")
                reply = await generate_reply(
                    admin_id, redis_client, deepseek_key,
                    user_text="(System: User 超過 4 小時沒回應。請主動傳訊關心，語氣擔心但不責備。)",
                    timer_trigger=True, minutes_since_last=minutes_since_last,
                )
                dm = await get_dm(admin_id)
                mode = state.get("length_mode", "normal")
                await send_bubbles(dm, reply, length_mode=mode)
                state["has_sent_care"] = True
                save_state(admin_id, state, redis_client)

        except Exception as e:
            logger.error(f"[heartbeat] 失敗: {e}")

    @tasks.loop(time=dt_time(21, 0, tzinfo=TZ))
    async def mood_loop():
        from tools.mood_tracker import update_mood_today
        logger.info("😶 開始更新今日情緒狀態……")
        try:
            await update_mood_today(redis_client, admin_id, deepseek_key)
        except Exception as e:
            logger.error(f"[mood] 定時更新失敗: {e}")

    @tasks.loop(time=dt_time(2, 0, tzinfo=TZ))
    async def daily_loop():
        from tools.mood_tracker import generate_daily_summary
        logger.info("📓 開始生成每日摘要……")
        try:
            await generate_daily_summary(redis_client, admin_id, deepseek_key)
        except Exception as e:
            logger.error(f"[daily] 定時摘要失敗: {e}")

    await bot.start(token)
