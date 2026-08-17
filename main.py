import asyncio
import json
import logging
import os
import random
import sqlite3
import string
import time
import requests
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
# NEW
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_ID = 7662143324
MAILTM_API = "https://api.mail.tm"
DB_FILE = "tempmail.db"
CYCLE_12H = 12 * 3600  # 12 Hours in seconds

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# DATABASE SYSTEM (SQLite)
# -------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                registered_at INTEGER,
                last_seen INTEGER
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS active_emails (
                user_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                token TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                seen_ids TEXT NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS email_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                email TEXT,
                created_at INTEGER
            )"""
        )
        conn.commit()


def touch_user(user_id: int):
    now = int(time.time())
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (user_id, registered_at, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = ?",
            (user_id, now, now, now),
        )
        conn.commit()


def get_active_email(user_id: int):
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM active_emails WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return dict(row) if row else None


def save_active_email(user_id: int, email: str, token: str, expires_at: int):
    now = int(time.time())
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO active_emails (user_id, email, token, created_at, expires_at, seen_ids)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   email=excluded.email, token=excluded.token,
                   created_at=excluded.created_at, expires_at=excluded.expires_at,
                   seen_ids=excluded.seen_ids""",
            (user_id, email, token, now, expires_at, json.dumps([])),
        )
        c.execute(
            "INSERT INTO email_history (user_id, email, created_at) VALUES (?, ?, ?)",
            (user_id, email, now),
        )
        conn.commit()


def update_seen_ids(user_id: int, seen_ids: list):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE active_emails SET seen_ids = ? WHERE user_id = ?",
            (json.dumps(seen_ids), user_id),
        )
        conn.commit()


def get_all_active_emails():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM active_emails")
        return [dict(r) for r in c.fetchall()]


def get_all_users():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        return [r["user_id"] for r in c.fetchall()]


def get_stats():
    now = int(time.time())
    d24 = now - 86400
    m30 = now - (30 * 86400)
    with get_db() as conn:
        c = conn.cursor()
        total_u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        daily_u = c.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?", (d24,)
        ).fetchone()[0]
        monthly_u = c.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?", (m30,)
        ).fetchone()[0]
        active_m = c.execute("SELECT COUNT(*) FROM active_emails").fetchone()[0]
        total_m = c.execute("SELECT COUNT(*) FROM email_history").fetchone()[0]
    return total_u, daily_u, monthly_u, active_m, total_m


# -------------------------------------------------------------------
# MAIL.TM API
# -------------------------------------------------------------------
def get_domain():
    try:
        r = requests.get(f"{MAILTM_API}/domains", timeout=10)
        if r.status_code == 200:
            doms = r.json().get("hydra:member", [])
            if doms:
                return doms[0]["domain"]
    except Exception as e:
        logger.error(f"Domain Fetch Error: {e}")
    return None


def create_mailtm_acc():
    dom = get_domain()
    if not dom:
        return None, None
    usr = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{usr}@{dom}"
    pwd = "".join(random.choices(string.ascii_letters + string.digits, k=12))

    try:
        r1 = requests.post(
            f"{MAILTM_API}/accounts",
            json={"address": email, "password": pwd},
            timeout=10,
        )
        if r1.status_code == 201:
            r2 = requests.post(
                f"{MAILTM_API}/token",
                json={"address": email, "password": pwd},
                timeout=10,
            )
            if r2.status_code == 200:
                return email, r2.json().get("token")
    except Exception as e:
        logger.error(f"Create Acc Error: {e}")
    return None, None


def get_inbox(token: str):
    try:
        r = requests.get(
            f"{MAILTM_API}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("hydra:member", [])
    except Exception as e:
        logger.error(f"Inbox Fetch Error: {e}")
    return []


def get_msg_detail(msg_id: str, token: str):
    try:
        r = requests.get(
            f"{MAILTM_API}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.error(f"Detail Fetch Error: {e}")
    return None


# -------------------------------------------------------------------
# HEALTH CHECK SERVER (UPTIMEROBOT)
# -------------------------------------------------------------------
async def handle_health(request):
    return web.Response(text="OK - Temp Mail Bot Online", status=200)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"⚡ Health check server live on port {port}")


# -------------------------------------------------------------------
# BACKGROUND POLLER & 12H ROTATOR
# -------------------------------------------------------------------
async def background_worker(app: Application):
    logger.info("⚡ Background Poller & Rotator active.")
    while True:
        try:
            records = get_all_active_emails()
            now = int(time.time())

            for rec in records:
                uid = rec["user_id"]
                token = rec["token"]
                expires_at = rec["expires_at"]
                seen_ids = json.loads(rec["seen_ids"])

                # 12-Hour Rotation Check
                if now >= expires_at:
                    n_email, n_token = create_mailtm_acc()
                    if n_email and n_token:
                        save_active_email(uid, n_email, n_token, now + CYCLE_12H)
                        msg = (
                            "🔄 **12-Hour Mail Rotated!**\n\n"
                            f"📧 **New Mail:** `{n_email}`\n"
                            "⏳ **Valid:** 12 Hours (2 Mails / Day)"
                        )
                        kbd = InlineKeyboardMarkup(
                            [[InlineKeyboardButton("📥 Refresh Inbox", callback_data="check_inbox")]]
                        )
                        try:
                            await app.bot.send_message(
                                chat_id=uid,
                                text=msg,
                                parse_mode="Markdown",
                                reply_markup=kbd,
                            )
                        except Exception:
                            pass
                    continue

                # Poll Inbox
                msgs = get_inbox(token)
                new_found = False
                for m in msgs:
                    mid = m.get("id")
                    if mid and mid not in seen_ids:
                        seen_ids.append(mid)
                        new_found = True
                        det = get_msg_detail(mid, token)
                        if det:
                            sender = det.get("from", {}).get("address", "Unknown")
                            subj = det.get("subject", "No Subject")
                            body = det.get("intro", det.get("text", "No Body"))
                            date = det.get("createdAt", "")[:19].replace("T", " ")

                            mail_text = (
                                "📩 **NEW MAIL RECEIVED**\n\n"
                                f"👤 **From:** `{sender}`\n"
                                f"📌 **Subject:** {subj}\n"
                                f"📅 **Date:** {date}\n\n"
                                f"💬 **Message:**\n{body[:1500]}"
                            )
                            kbd = InlineKeyboardMarkup(
                                [[InlineKeyboardButton("📥 Refresh Inbox", callback_data="check_inbox")]]
                            )
                            try:
                                await app.bot.send_message(
                                    chat_id=uid,
                                    text=mail_text,
                                    parse_mode="Markdown",
                                    reply_markup=kbd,
                                )
                            except Exception as e:
                                logger.error(f"Send mail to {uid} failed: {e}")

                if new_found:
                    update_seen_ids(uid, seen_ids)

        except Exception as e:
            logger.error(f"Poller Error: {e}")

        await asyncio.sleep(5)


# -------------------------------------------------------------------
# HELPER & TELEGRAM HANDLERS
# -------------------------------------------------------------------
def get_time_left(expires_at: int) -> str:
    rem = expires_at - int(time.time())
    if rem <= 0:
        return "0h 0m"
    return f"{rem // 3600}h {(rem % 3600) // 60}m"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    touch_user(uid)

    rec = get_active_email(uid)
    now = int(time.time())

    if not rec or now >= rec["expires_at"]:
        email, token = create_mailtm_acc()
        if not email or not token:
            await update.message.reply_text("❌ Failed to allocate email. Try again.")
            return
        save_active_email(uid, email, token, now + CYCLE_12H)
        rec = get_active_email(uid)

    t_left = get_time_left(rec["expires_at"])
    txt = (
        "⚡ **Temp Mail Active**\n\n"
        f"📧 **Your Mail:** `{rec['email']}`\n"
        f"⏳ **Expires In:** `{t_left}`\n"
        "🔄 **Schedule:** 2 Mails / 24 Hours"
    )

    btns = [
        [InlineKeyboardButton("📥 Refresh Inbox", callback_data="check_inbox")],
        [InlineKeyboardButton("👤 My Status", callback_data="my_status")],
    ]
    if uid == ADMIN_ID:
        btns.append([InlineKeyboardButton("📊 Admin Dashboard", callback_data="admin_stats")])

    await update.message.reply_text(
        txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns)
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return  # Admin Only Silent Block

    tot_u, d_u, m_u, act_m, tot_m = get_stats()
    txt = (
        "📊 **ADMIN DASHBOARD**\n\n"
        f"👥 **Total Users:** `{tot_u}`\n"
        f"🔥 **24H Active:** `{d_u}`\n"
        f"📅 **30D Monthly:** `{m_u}`\n"
        f"⚡ **Active Mails:** `{act_m}`\n"
        f"📧 **Total Generated:** `{tot_m}`"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return  # Admin Only Protection

    msg_text = " ".join(context.args)
    if not msg_text:
        await update.message.reply_text(
            "⚠️ **Usage:** `/broadcast Your message here`", parse_mode="Markdown"
        )
        return

    users = get_all_users()
    sent = 0
    failed = 0

    status_msg = await update.message.reply_text(f"📢 **Broadcasting to {len(users)} users...**")

    for u in users:
        try:
            await context.bot.send_message(
                chat_id=u, text=f"📢 **ANNOUNCEMENT**\n\n{msg_text}", parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)  # Telegram Rate Limit Safety
        except TelegramError:
            failed += 1

    await status_msg.edit_text(
        f"✅ **Broadcast Completed!**\n\n"
        f"📤 **Sent:** `{sent}`\n"
        f"❌ **Failed/Blocked:** `{failed}`",
        parse_mode="Markdown",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    touch_user(uid)

    try:
        if query.data == "check_inbox":
            rec = get_active_email(uid)
            if not rec:
                await query.answer("❌ No active email found!", show_alert=True)
                return
            msgs = get_inbox(rec["token"])
            if not msgs:
                await query.answer("📥 Inbox empty. No emails yet!", show_alert=False)
            else:
                await query.answer(f"🔔 {len(msgs)} email(s) found in inbox!", show_alert=True)

        elif query.data == "my_status":
            rec = get_active_email(uid)
            if not rec:
                await query.answer("❌ Active session expired.", show_alert=True)
                return
            t_left = get_time_left(rec["expires_at"])
            txt = (
                "👤 **USER STATUS**\n\n"
                f"🆔 **User ID:** `{uid}`\n"
                f"📧 **Mail:** `{rec['email']}`\n"
                f"⏳ **Next Rotation:** `{t_left}`\n"
                "⚙️ **Quota:** 2 Mails / 24 Hours"
            )
            kbd = [
                [InlineKeyboardButton("📥 Refresh Inbox", callback_data="check_inbox")]
            ]
            if uid == ADMIN_ID:
                kbd.append([InlineKeyboardButton("📊 Admin Dashboard", callback_data="admin_stats")])
            await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))

        elif query.data == "admin_stats":
            if uid != ADMIN_ID:
                await query.answer("⛔ Access Denied! Admin Only.", show_alert=True)
                return
            tot_u, d_u, m_u, act_m, tot_m = get_stats()
            txt = (
                "📊 **ADMIN DASHBOARD**\n\n"
                f"👥 **Total Users:** `{tot_u}`\n"
                f"🔥 **24H Active:** `{d_u}`\n"
                f"📅 **30D Monthly:** `{m_u}`\n"
                f"⚡ **Active Mails:** `{act_m}`\n"
                f"📧 **Total Generated:** `{tot_m}`"
            )
            kbd = [[InlineKeyboardButton("⬅️ Back", callback_data="my_status")]]
            await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))

    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"BadRequest: {e}")


# -------------------------------------------------------------------
# MAIN STARTUP
# -------------------------------------------------------------------
async def post_init(app: Application):
    await start_health_server()
    asyncio.create_task(background_worker(app))


def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("bc", broadcast_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("⚡ Bot initialized successfully!")
    app.run_polling()


if __name__ == "__main__":
    main()
