import logging
import sqlite3
import re
import html
import random
import httpx
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, JobQueue

# ---------- CONFIG ----------
BOT_TOKEN = "8949615977:AAGr7oJagOGpgtq_t_AgJWXOd5Sj25mrmcY"
OWNER_ID = 8477195695
CO_OWNER_IDS = [8477195695]
WARN_LIMIT = 3
MUTE_DURATION_SECONDS = 3600
CLEANUP_INTERVAL = 59

# ---------- OPENAI API KEY (CORRECT ENDPOINT & MODEL) ----------
OPENAI_API_KEY = "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"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"  # Safer, widely available

# ---------- NO FALLBACK REPLIES – ONLY API OR ERROR ----------

BADWORD_REPLIES = [
    "Arey, kya gaali de rahe ho? 😾 Sharam karo! Aise baat nahi karte.",
    "Itna gussa? Chalo, thoda pyaar do 🤗 Gaali mat do.",
    "Gaali nahi, pyaar se baat karo 💋 Nahi toh main hurt ho jaungi.",
    "Mere saamne gaali? 😠 Delete kar diya, ab aage se dhyan rakho 🧐",
    "Chup chaap raho, warna mute kar dungi 😈",
    "Aise baat karte ho? Mujhe bura laga 😭 kya mila gaali deke?",
    "Kya bol rahe ho? Delete kiya, next time warn hoge 🤨",
    "Isko gaali nahi kehte, isko na-insaafi kehte hain 😤",
    "Maine aapka message delete kar diya, kyunki main aapki maa hoon 😂",
    "Thoda tameez se baat karo, warna main block kar dungi 😎",
    "Gaali mat do, warna mummy ko complain kar dungi 😠",
    "Arre bhai, itna gussa? Chillaao mat, thoda pyar karo 💖",
    "Ye kya bakwas kar rahe ho? Delete kiya, ab shant ho jao 🥱",
    "Mujhe gaaliyan pasand nahi, tumhe bhi nahi deni chahiye 😤",
    "Maine aapki message delete kar di, kyunki main aapki dost hoon 🥺"
]

STICKERS = [
    "CAACAgQAAxkBAAED2TVl7HmHxYz...",
    "CAACAgQAAxkBAAED2TZl7HmHxYz...",
    "CAACAgQAAxkBAAED2Tdl7HmHxYz..."
]

# ---------- DATABASE ----------
conn = sqlite3.connect("oggy_data.db", check_same_thread=False)
c = conn.cursor()
try:
    c.execute("ALTER TABLE group_settings ADD COLUMN girl_mode INTEGER DEFAULT 1")
except sqlite3.OperationalError:
    pass
c.execute('''CREATE TABLE IF NOT EXISTS warns (user_id INTEGER, chat_id INTEGER, count INTEGER, PRIMARY KEY (user_id, chat_id))''')
c.execute('''CREATE TABLE IF NOT EXISTS mutes (user_id INTEGER, chat_id INTEGER, until INTEGER, PRIMARY KEY (user_id, chat_id))''')
c.execute('''CREATE TABLE IF NOT EXISTS user_roles (user_id INTEGER, chat_id INTEGER, role TEXT, PRIMARY KEY (user_id, chat_id))''')
c.execute('''CREATE TABLE IF NOT EXISTS group_settings (chat_id INTEGER PRIMARY KEY, link_guard INTEGER DEFAULT 1, dm_guard INTEGER DEFAULT 1, badword_guard INTEGER DEFAULT 1, cleanup_enabled INTEGER DEFAULT 1, log_channel INTEGER DEFAULT 0, girl_mode INTEGER DEFAULT 1)''')
c.execute('''CREATE TABLE IF NOT EXISTS bad_words (chat_id INTEGER, word TEXT, PRIMARY KEY (chat_id, word))''')
conn.commit()

# ---------- HELPERS ----------
def get_setting(chat_id: int, key: str) -> bool:
    try:
        c.execute(f"SELECT {key} FROM group_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row is None:
            defaults = {"link_guard": 1, "dm_guard": 1, "badword_guard": 1, "cleanup_enabled": 1, "girl_mode": 1}
            val = defaults.get(key, 1)
            c.execute("INSERT INTO group_settings (chat_id, link_guard, dm_guard, badword_guard, cleanup_enabled, log_channel, girl_mode) VALUES (?,1,1,1,1,0,1)", (chat_id,))
            conn.commit()
            return bool(val)
        return bool(row[0])
    except:
        return True

def set_setting(chat_id: int, key: str, value: bool):
    c.execute(f"UPDATE group_settings SET {key}=? WHERE chat_id=?", (1 if value else 0, chat_id))
    conn.commit()

def get_log_channel(chat_id: int) -> int:
    c.execute("SELECT log_channel FROM group_settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    return row[0] if row else 0

def set_log_channel(chat_id: int, log_id: int):
    c.execute("INSERT OR REPLACE INTO group_settings (chat_id, log_channel) VALUES (?,?)", (chat_id, log_id))
    conn.commit()

def get_warn_count(user_id: int, chat_id: int) -> int:
    c.execute("SELECT count FROM warns WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    row = c.fetchone()
    return row[0] if row else 0

def add_warn(user_id: int, chat_id: int) -> int:
    cur = get_warn_count(user_id, chat_id)
    if cur == 0:
        c.execute("INSERT INTO warns (user_id, chat_id, count) VALUES (?,?,1)", (user_id, chat_id))
    else:
        c.execute("UPDATE warns SET count=? WHERE user_id=? AND chat_id=?", (cur+1, user_id, chat_id))
    conn.commit()
    return cur+1

def reset_warns(user_id: int, chat_id: int):
    c.execute("DELETE FROM warns WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()

def get_all_warns(chat_id: int) -> List[tuple]:
    c.execute("SELECT user_id, count FROM warns WHERE chat_id=?", (chat_id,))
    return c.fetchall()

def set_mute(user_id: int, chat_id: int, duration_sec: int):
    until = int((datetime.now() + timedelta(seconds=duration_sec)).timestamp())
    c.execute("INSERT OR REPLACE INTO mutes (user_id, chat_id, until) VALUES (?,?,?)", (user_id, chat_id, until))
    conn.commit()

def remove_mute(user_id: int, chat_id: int):
    c.execute("DELETE FROM mutes WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()

def is_muted(user_id: int, chat_id: int) -> bool:
    c.execute("SELECT until FROM mutes WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    row = c.fetchone()
    if row:
        if row[0] > int(datetime.now().timestamp()):
            return True
        else:
            remove_mute(user_id, chat_id)
    return False

def get_role(user_id: int, chat_id: int) -> str:
    c.execute("SELECT role FROM user_roles WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    row = c.fetchone()
    return row[0] if row else "Free"

def set_role(user_id: int, chat_id: int, role: str):
    c.execute("INSERT OR REPLACE INTO user_roles (user_id, chat_id, role) VALUES (?,?,?)", (user_id, chat_id, role))
    conn.commit()

def remove_role(user_id: int, chat_id: int):
    c.execute("DELETE FROM user_roles WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()

def is_founder(user_id: int) -> bool:
    return user_id == OWNER_ID

def is_cofounder(user_id: int) -> bool:
    return user_id in CO_OWNER_IDS

async def is_admin(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_founder(user_id) or is_cofounder(user_id):
        return True
    try:
        admins = context.bot_data.get(f"admins_{chat_id}")
        if admins is None:
            admins = [a.user.id for a in await context.bot.get_chat_administrators(chat_id)]
            context.bot_data[f"admins_{chat_id}"] = admins
        return user_id in admins
    except:
        return False

def has_role(user_id: int, chat_id: int, required_roles: List[str]) -> bool:
    role = get_role(user_id, chat_id)
    return role in required_roles

async def can_use_admin_cmd(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return await is_admin(user_id, chat_id, context)

async def can_use_mod_cmd(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await can_use_admin_cmd(user_id, chat_id, context):
        return True
    return has_role(user_id, chat_id, ["Moderator"])

async def can_use_cleaner_cmd(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await can_use_admin_cmd(user_id, chat_id, context):
        return True
    return has_role(user_id, chat_id, ["Cleaner"])

async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(OWNER_ID, text)
    for co in CO_OWNER_IDS:
        await context.bot.send_message(co, text)

def get_roles_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("👑 Founder", callback_data=f"role_Founder_{chat_id}"),
         InlineKeyboardButton("👑 Co-Founder", callback_data=f"role_Co-Founder_{chat_id}")],
        [InlineKeyboardButton("🛡 Admin", callback_data=f"role_Admin_{chat_id}"),
         InlineKeyboardButton("⚔️ Moderator", callback_data=f"role_Moderator_{chat_id}")],
        [InlineKeyboardButton("🧹 Cleaner", callback_data=f"role_Cleaner_{chat_id}"),
         InlineKeyboardButton("🔇 Muter", callback_data=f"role_Muter_{chat_id}")],
        [InlineKeyboardButton("🤝 Helper", callback_data=f"role_Helper_{chat_id}"),
         InlineKeyboardButton("🆓 Free (Remove)", callback_data=f"role_Free_{chat_id}")]
    ]
    return InlineKeyboardMarkup(buttons)

def get_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    lg = "✅" if get_setting(chat_id, "link_guard") else "❌"
    dg = "✅" if get_setting(chat_id, "dm_guard") else "❌"
    bw = "✅" if get_setting(chat_id, "badword_guard") else "❌"
    cl = "✅" if get_setting(chat_id, "cleanup_enabled") else "❌"
    gm = "✅" if get_setting(chat_id, "girl_mode") else "❌"
    buttons = [
        [InlineKeyboardButton(f"{lg} Link Guard", callback_data=f"set_link_{chat_id}"),
         InlineKeyboardButton(f"{dg} DM Guard", callback_data=f"set_dm_{chat_id}")],
        [InlineKeyboardButton(f"{bw} BadWord Guard", callback_data=f"set_badword_{chat_id}"),
         InlineKeyboardButton(f"{cl} Auto-Cleanup", callback_data=f"set_cleanup_{chat_id}")],
        [InlineKeyboardButton(f"{gm} Girl Mode", callback_data=f"set_girl_{chat_id}"),
         InlineKeyboardButton("📝 Set Log Channel", callback_data=f"set_log_{chat_id}")]
    ]
    return InlineKeyboardMarkup(buttons)

# ---------- MAINTENANCE MODE ----------
MAINTENANCE_KEY = "maintenance_mode"

async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner use kar sakta hai.")
        return
    args = context.args
    if not args:
        current = context.bot_data.get(MAINTENANCE_KEY, False)
        status = "ON" if current else "OFF"
        await update.message.reply_text(f"🔧 Maintenance mode: {status}")
        return
    if args[0].lower() == "on":
        context.bot_data[MAINTENANCE_KEY] = True
        await update.message.reply_text("🔧 Maintenance mode ENABLED. Bot sirf admin/owner commands chalayega.")
    elif args[0].lower() == "off":
        context.bot_data[MAINTENANCE_KEY] = False
        await update.message.reply_text("🔧 Maintenance mode DISABLED. Bot normal chalega.")
    else:
        await update.message.reply_text("Usage: /maintenance on/off")

# ---------- COMMAND HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 **OGGYAI ULTIMATE HELPER + GIRL AI**\n\n"
        "👮 /modcmds - Admin/Mod Commands\n"
        "🕵️ /admincmds - Admin only\n"
        "🛃 /cleanercmds - Cleaner only\n"
        "◽ /me - Your info\n"
        "⚙️ /settings - Group Settings\n"
        "👥 /roles - Manage User Roles\n"
        "💁‍♀️ /girlmode on/off - Toggle Girl AI\n"
        "🔧 /maintenance on/off - Owner only", parse_mode="Markdown"
    )

async def modcmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("👮 **Admin & Moderator Commands**\n\n"
           "/ban - Ban user\n"
           "/mute - Mute user\n"
           "/kick - Kick user\n"
           "/unban - Unban user\n"
           "/info - User info\n"
           "/infopvt - Info in private\n"
           "/staff - Staff list\n"
           "/warn - Warn user\n"
           "/unwarn - Remove warn\n"
           "/warns - View warns\n"
           "/delwarn - Delete msg + warn\n"
           "/intervention - Call support")
    await update.message.reply_text(txt, parse_mode="Markdown")

async def admincmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("🕵️ **Admin Only Commands**\n\n"
           "/reload - Reload admin list\n"
           "/settings - Bot settings\n"
           "/send - Send HTML message")
    await update.message.reply_text(txt, parse_mode="Markdown")

async def cleanercmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = ("🛃 **Admin & Cleaner Commands**\n\n"
           "/del - Delete message\n"
           "/logdel - Delete & log to channel")
    await update.message.reply_text(txt, parse_mode="Markdown")

async def roles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_admin_cmd(update.effective_user.id, update.effective_chat.id, context):
        return await update.message.reply_text("❌ Sirf Admin/Founder/Co-Founder.")
    await update.message.reply_text("Select a role to assign:\n(Reply to a user after selecting)",
                                    reply_markup=get_roles_keyboard(update.effective_chat.id))

async def roles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    role = data[1]
    chat_id = int(data[2])
    if not await can_use_admin_cmd(query.from_user.id, chat_id, context):
        return await query.edit_message_text("❌ No permission.")
    context.user_data['pending_role'] = role
    context.user_data['target_chat'] = chat_id
    await query.edit_message_text(f"📌 Role `{role}` selected. Now **reply** to the user's message with anything to assign this role.", parse_mode="Markdown")

async def handle_role_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'pending_role' not in context.user_data:
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Reply to a user's message!")
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    role = context.user_data['pending_role']
    if role == "Free":
        remove_role(target.id, chat_id)
        await update.message.reply_text(f"✅ Role removed for {target.mention_html()}", parse_mode="HTML")
    else:
        set_role(target.id, chat_id, role)
        await update.message.reply_text(f"✅ {target.mention_html()} assigned as `{role}`", parse_mode="HTML")
    del context.user_data['pending_role']

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_admin_cmd(update.effective_user.id, update.effective_chat.id, context):
        return
    await update.message.reply_text("⚙️ **Settings**", reply_markup=get_settings_keyboard(update.effective_chat.id), parse_mode="Markdown")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action = data[1]
    chat_id = int(data[2])
    if not await can_use_admin_cmd(query.from_user.id, chat_id, context):
        return await query.edit_message_text("❌ No permission.")
    if action == "log":
        await query.edit_message_text("📝 Send me the **Log Channel ID** now.\n(Reply to this message with the ID)")
        context.user_data['awaiting_log'] = chat_id
        return
    setting_map = {"link": "link_guard", "dm": "dm_guard", "badword": "badword_guard", "cleanup": "cleanup_enabled", "girl": "girl_mode"}
    key = setting_map.get(action)
    if key:
        val = not get_setting(chat_id, key)
        set_setting(chat_id, key, val)
    await query.edit_message_text("⚙️ Settings updated.", reply_markup=get_settings_keyboard(chat_id))

async def handle_log_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'awaiting_log' not in context.user_data:
        return
    chat_id = context.user_data['awaiting_log']
    if not await can_use_admin_cmd(update.effective_user.id, chat_id, context):
        return
    try:
        log_id = int(update.message.text)
        set_log_channel(chat_id, log_id)
        await update.message.reply_text(f"✅ Log Channel set to {log_id}")
    except:
        await update.message.reply_text("❌ Invalid ID. Send numeric ID.")
    del context.user_data['awaiting_log']

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to user with /warn reason")
    user = update.message.reply_to_message.from_user
    chat = update.effective_chat
    if user.id == OWNER_ID or user.id in CO_OWNER_IDS:
        return await update.message.reply_text("Owner ko warn nahi kar sakta.")
    if await is_admin(user.id, chat.id, context):
        return await update.message.reply_text("Admin ko warn nahi.")
    reason = " ".join(context.args) if context.args else "No reason"
    count = add_warn(user.id, chat.id)
    await update.message.reply_text(f"⚠️ {user.mention_html()} warned! ({count}/{WARN_LIMIT})\nReason: {reason}", parse_mode="HTML")
    await notify_owner(context, f"⚠️ Warn: {user.full_name} (ID: {user.id}) in {chat.title or 'group'} for '{reason}' (count: {count})")
    if count >= WARN_LIMIT:
        set_mute(user.id, chat.id, MUTE_DURATION_SECONDS)
        await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(can_send_messages=False))
        await update.message.reply_text(f"🔇 {user.mention_html()} muted for 1 hour (3 warns).", parse_mode="HTML")
        await notify_owner(context, f"🔇 Muted: {user.full_name} (ID: {user.id}) in {chat.title or 'group'} for 1 hour (3 warns).")
        reset_warns(user.id, chat.id)

async def unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context):
        return
    if not update.message.reply_to_message:
        return
    user = update.message.reply_to_message.from_user
    reset_warns(user.id, update.effective_chat.id)
    await update.message.reply_text(f"✅ Warns reset for {user.mention_html()}.", parse_mode="HTML")
    await notify_owner(context, f"✅ Warns reset for {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")

async def warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context):
        return
    chat_id = update.effective_chat.id
    all_w = get_all_warns(chat_id)
    if not all_w:
        return await update.message.reply_text("No warns in this group.")
    txt = "📊 **Warn List:**\n"
    for uid, cnt in all_w:
        try:
            user = await context.bot.get_chat(uid)
            name = user.full_name or str(uid)
        except:
            name = str(uid)
        txt += f"• {html.escape(name)}: {cnt}/{WARN_LIMIT}\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def delwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context):
        return
    if not update.message.reply_to_message:
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.message.reply_to_message.delete()
    except:
        pass
    count = add_warn(user.id, update.effective_chat.id)
    await update.message.reply_text(f"🗑️ Deleted msg & warned {user.mention_html()} ({count}/{WARN_LIMIT})", parse_mode="HTML")
    if count >= WARN_LIMIT:
        set_mute(user.id, update.effective_chat.id, MUTE_DURATION_SECONDS)
        await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False))
        await update.message.reply_text(f"🔇 {user.mention_html()} muted.", parse_mode="HTML")
        reset_warns(user.id, update.effective_chat.id)
        await notify_owner(context, f"🔇 Muted (delwarn): {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message: return
    user = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    await update.message.reply_text(f"🔨 {user.mention_html()} banned.", parse_mode="HTML")
    await notify_owner(context, f"🔨 Banned: {user.full_name} (ID: {user.id}) from {update.effective_chat.title or 'group'}")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message: return
    user = update.message.reply_to_message.from_user
    await context.bot.unban_chat_member(update.effective_chat.id, user.id)
    await update.message.reply_text(f"🔓 {user.mention_html()} unbanned.", parse_mode="HTML")
    await notify_owner(context, f"🔓 Unbanned: {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message: return
    user = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(update.effective_chat.id, user.id)
    await context.bot.unban_chat_member(update.effective_chat.id, user.id)
    await update.message.reply_text(f"👢 {user.mention_html()} kicked.", parse_mode="HTML")
    await notify_owner(context, f"👢 Kicked: {user.full_name} (ID: {user.id}) from {update.effective_chat.title or 'group'}")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message: return
    user = update.message.reply_to_message.from_user
    duration = int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    set_mute(user.id, update.effective_chat.id, duration*60)
    await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False))
    await update.message.reply_text(f"🔇 {user.mention_html()} muted for {duration} min.", parse_mode="HTML")
    await notify_owner(context, f"🔇 Muted: {user.full_name} (ID: {user.id}) for {duration} min in {update.effective_chat.title or 'group'}")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message: return
    user = update.message.reply_to_message.from_user
    remove_mute(user.id, update.effective_chat.id)
    await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True))
    await update.message.reply_text(f"🔊 {user.mention_html()} unmuted.", parse_mode="HTML")
    await notify_owner(context, f"🔊 Unmuted: {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    role = get_role(target.id, update.effective_chat.id)
    warns = get_warn_count(target.id, update.effective_chat.id)
    txt = f"👤 **Info**\nID: `{target.id}`\nRole: {role}\nWarns: {warns}/{WARN_LIMIT}"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def infopvt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    role = get_role(target.id, update.effective_chat.id)
    warns = get_warn_count(target.id, update.effective_chat.id)
    txt = f"👤 **Info (Private)**\nID: `{target.id}`\nRole: {role}\nWarns: {warns}/{WARN_LIMIT}"
    await context.bot.send_message(target.id, txt, parse_mode="Markdown")

async def staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    chat_id = update.effective_chat.id
    admins = await context.bot.get_chat_administrators(chat_id)
    txt = "👥 **Staff List**\n"
    for a in admins:
        txt += f"• {a.user.full_name} ({a.status})\n"
    c.execute("SELECT user_id, role FROM user_roles WHERE chat_id=?", (chat_id,))
    custom = c.fetchall()
    for uid, role in custom:
        if uid in [a.user.id for a in admins]:
            continue
        try:
            user = await context.bot.get_chat(uid)
            name = user.full_name
        except:
            name = str(uid)
        txt += f"• {name} ({role})\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def intervention(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_mod_cmd(update.effective_user.id, update.effective_chat.id, context): return
    txt = f"🚨 Intervention requested by {update.effective_user.mention_html()} in {update.effective_chat.title or 'Group'}!"
    await context.bot.send_message(OWNER_ID, txt, parse_mode="HTML")
    for co in CO_OWNER_IDS:
        await context.bot.send_message(co, txt, parse_mode="HTML")
    await update.message.reply_text("✅ Support notified.")

async def reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_admin_cmd(update.effective_user.id, update.effective_chat.id, context): return
    context.bot_data.pop(f"admins_{update.effective_chat.id}", None)
    await update.message.reply_text("🔄 Admin list reloaded.")

async def send_html(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_admin_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not context.args:
        return await update.message.reply_text("Usage: /send <b>Hello</b>")
    text = " ".join(context.args)
    await update.message.reply_text(text, parse_mode="HTML")

async def delete_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_cleaner_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message:
        return
    await update.message.reply_to_message.delete()
    await update.message.reply_text("🗑️ Deleted.")

async def logdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_cleaner_cmd(update.effective_user.id, update.effective_chat.id, context): return
    if not update.message.reply_to_message:
        return
    msg = update.message.reply_to_message
    log_id = get_log_channel(update.effective_chat.id)
    if log_id:
        try:
            await context.bot.forward_message(log_id, update.effective_chat.id, msg.message_id)
        except:
            pass
    await msg.delete()
    await update.message.reply_text("🗑️ Deleted & Logged.")

async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = get_role(user.id, update.effective_chat.id)
    warns = get_warn_count(user.id, update.effective_chat.id)
    txt = f"👤 **Your Info**\nID: `{user.id}`\nRole: {role}\nWarns: {warns}/{WARN_LIMIT}"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def girlmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await can_use_admin_cmd(update.effective_user.id, update.effective_chat.id, context):
        return await update.message.reply_text("❌ Sirf Admin/Founder/Co-Founder.")
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        current = get_setting(chat_id, "girl_mode")
        status = "ON" if current else "OFF"
        return await update.message.reply_text(f"💁‍♀️ Girl mode is currently: {status}\nTo toggle: /girlmode on/off")
    if args[0].lower() == "on":
        set_setting(chat_id, "girl_mode", True)
        await update.message.reply_text("💁‍♀️ Girl mode enabled! Ab main OpenAI se reply dungi 😘")
    elif args[0].lower() == "off":
        set_setting(chat_id, "girl_mode", False)
        await update.message.reply_text("💁‍♀️ Girl mode disabled. Ab main normal helper ban jaungi 😐")
    else:
        await update.message.reply_text("Usage: /girlmode on/off")

message_store: Dict[int, List[int]] = {}

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return
    if not get_setting(update.effective_chat.id, "cleanup_enabled"):
        return
    if update.message:
        msg_id = update.message.message_id
        chat_id = update.effective_chat.id
        if chat_id not in message_store:
            message_store[chat_id] = []
        message_store[chat_id].append(msg_id)
        if len(message_store[chat_id]) > 5000:
            message_store[chat_id] = message_store[chat_id][-5000:]

async def cleanup_messages(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, msg_ids in message_store.items():
        if not msg_ids: continue
        if not get_setting(chat_id, "cleanup_enabled"):
            continue
        try:
            for i in range(0, len(msg_ids), 100):
                batch = msg_ids[i:i+100]
                for mid in batch:
                    try:
                        await context.bot.delete_message(chat_id, mid)
                    except:
                        pass
            message_store[chat_id] = []
        except Exception as e:
            logging.error(f"Cleanup error {chat_id}: {e}")

async def filter_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return
    if not update.message: return
    if not get_setting(update.effective_chat.id, "link_guard"):
        return
    user = update.effective_user
    if await can_use_mod_cmd(user.id, update.effective_chat.id, context):
        return
    text = update.message.text or update.message.caption or ""
    if re.search(r'(http|t\.me|telegram)', text, re.I):
        try:
            await update.message.delete()
        except: pass
        count = add_warn(user.id, update.effective_chat.id)
        await update.message.reply_text(f"🚫 Link daalna mana! Warn {count}/{WARN_LIMIT}")
        if count >= WARN_LIMIT:
            set_mute(user.id, update.effective_chat.id, MUTE_DURATION_SECONDS)
            await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False))
            await update.message.reply_text(f"🔇 {user.mention_html()} muted for 1 hour (link spam).", parse_mode="HTML")
            reset_warns(user.id, update.effective_chat.id)
            await notify_owner(context, f"🔇 Muted (links): {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")

async def filter_badwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return
    if not update.message: return
    if not get_setting(update.effective_chat.id, "badword_guard"):
        return
    user = update.effective_user
    if await can_use_mod_cmd(user.id, update.effective_chat.id, context):
        return
    text = update.message.text or ""
    c.execute("SELECT word FROM bad_words WHERE chat_id=?", (update.effective_chat.id,))
    bads = [row[0] for row in c.fetchall()]
    for bw in bads:
        if bw.lower() in text.lower():
            try:
                await update.message.delete()
            except: pass
            count = add_warn(user.id, update.effective_chat.id)
            warning = random.choice(BADWORD_REPLIES)
            await update.message.reply_text(f"{warning}\n⚠️ Warn {count}/{WARN_LIMIT}")
            if count >= WARN_LIMIT:
                set_mute(user.id, update.effective_chat.id, MUTE_DURATION_SECONDS)
                await context.bot.restrict_chat_member(update.effective_chat.id, user.id, ChatPermissions(can_send_messages=False))
                await update.message.reply_text(f"🔇 {user.mention_html()} muted for 1 hour (bad words).", parse_mode="HTML")
                reset_warns(user.id, update.effective_chat.id)
                await notify_owner(context, f"🔇 Muted (bad words): {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")
            break

last_msg: Dict[int, Dict[int, str]] = {}
spam_warns: Dict[int, Dict[int, int]] = {}

async def check_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user.id == context.bot.id or await can_use_mod_cmd(user.id, chat_id, context):
        return
    text = update.message.text
    if chat_id not in last_msg:
        last_msg[chat_id] = {}
    if user.id in last_msg[chat_id] and last_msg[chat_id][user.id] == text:
        if chat_id not in spam_warns:
            spam_warns[chat_id] = {}
        spam_warns[chat_id][user.id] = spam_warns[chat_id].get(user.id, 0) + 1
        if spam_warns[chat_id][user.id] >= 3:
            count = add_warn(user.id, chat_id)
            await update.message.delete()
            await update.message.reply_text(f"🚫 Spam mat karo! Warn {count}/{WARN_LIMIT}")
            if count >= WARN_LIMIT:
                set_mute(user.id, chat_id, MUTE_DURATION_SECONDS)
                await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
                await update.message.reply_text(f"🔇 {user.mention_html()} muted for 1 hour (spam).", parse_mode="HTML")
                reset_warns(user.id, chat_id)
                await notify_owner(context, f"🔇 Muted (spam): {user.full_name} (ID: {user.id}) in {update.effective_chat.title or 'group'}")
            spam_warns[chat_id][user.id] = 0
        else:
            await update.message.delete()
            await update.message.reply_text(f"⚠️ Same message repeated! Spam warning {spam_warns[chat_id][user.id]}/3")
    else:
        last_msg[chat_id][user.id] = text
        spam_warns[chat_id][user.id] = 0

# ---------- GIRL AUTO-REPLY – OPENAI API (CORRECT) ----------
async def girl_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        return
    if not update.message or not update.message.text:
        return
    if update.message.text.startswith("/"):
        return
    if update.effective_user.id == context.bot.id:
        return
    chat_id = update.effective_chat.id
    if not get_setting(chat_id, "girl_mode"):
        return

    # Check maintenance mode
    if context.bot_data.get(MAINTENANCE_KEY, False):
        await update.message.reply_text("🔧 Bot maintenance mode, please wait.")
        return

    user_msg = update.message.text

    # Hack detection – immediate reply (no API)
    if re.search(r'(hack|cheat|mod|bgmi|free fire|ff|injector|aimbot|wallhack|esp)', user_msg, re.I):
        reply = f"Arey bhai, hack ke liye @UROGGY ko DM karo 😏, woh expert hai. Main toh bas pyaar baantti hu 💖"
        await update.message.reply_text(reply)
        return

    # Prepare OpenAI chat completions payload
    prompt = f"Tu ek desi ladki hai jo Hinglish mein baat karti hai, emoji aur attitude ke saath. User ne kaha: '{user_msg}'. Chhota ya detailed reply dekh kar de – chhoti baat par chhota, badi baat par detailed."

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 200 if len(user_msg) < 20 else 300,
        "temperature": 0.85
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(OPENAI_URL, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                try:
                    reply = data['choices'][0]['message']['content'].strip()
                    reply = reply.strip('"').strip("'")
                    if len(reply) > 300:
                        reply = reply[:300] + "..."
                    await update.message.reply_text(reply)
                    if random.random() < 0.2 and STICKERS:
                        try:
                            await update.message.reply_sticker(random.choice(STICKERS))
                        except:
                            pass
                    return
                except Exception as e:
                    logging.error(f"Parsing OpenAI response: {e}")
                    await update.message.reply_text("API Error")
                    return
            else:
                logging.error(f"OpenAI API error: {response.status_code} - {response.text}")
                await update.message.reply_text("API Error")
                return
    except Exception as e:
        logging.error(f"OpenAI request failed: {e}")
        await update.message.reply_text("API Error")
        return

async def handle_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    if user.id == OWNER_ID or user.id in CO_OWNER_IDS:
        return
    try:
        await update.message.delete()
    except:
        pass
    count = add_warn(user.id, user.id)
    await context.bot.send_message(user.id, f"⚠️ DM karna mana hai! {count}/{WARN_LIMIT} warnings. Next time block.")
    if count >= WARN_LIMIT:
        reset_warns(user.id, user.id)
        await context.bot.block_user(user.id)
    await notify_owner(context, f"📩 DM attempt: {user.full_name} (ID: {user.id}) – warned ({count})")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data = {}
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modcmds", modcmds))
    app.add_handler(CommandHandler("admincmds", admincmds))
    app.add_handler(CommandHandler("cleanercmds", cleanercmds))
    app.add_handler(CommandHandler("roles", roles_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("reload", reload))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("infopvt", infopvt))
    app.add_handler(CommandHandler("staff", staff))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("unwarn", unwarn))
    app.add_handler(CommandHandler("warns", warns))
    app.add_handler(CommandHandler("delwarn", delwarn))
    app.add_handler(CommandHandler("intervention", intervention))
    app.add_handler(CommandHandler("send", send_html))
    app.add_handler(CommandHandler("del", delete_msg))
    app.add_handler(CommandHandler("logdel", logdel))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("girlmode", girlmode))
    app.add_handler(CommandHandler("maintenance", maintenance))
    app.add_handler(CallbackQueryHandler(roles_callback, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_dm))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, filter_links), group=2)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, filter_badwords), group=3)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, check_spam), group=4)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, girl_auto_reply), group=5)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, store_message), group=1)
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_role_reply), group=6)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_log_channel), group=7)
    job_queue = app.job_queue
    job_queue.run_repeating(cleanup_messages, interval=CLEANUP_INTERVAL*60, first=10)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    main()