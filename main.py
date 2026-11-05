import logging
import os
import sqlite3
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 开启日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ==========================================
# 🔐 私人专属配置
# ==========================================
BOT_USERNAME = "Yinhepan_bot"  # 你的自用机器人用户名
ADMIN_USER_ID = 0  # 填入你的 Telegram 数字 ID（若不确定可先留0，看日志绑定）
# ==========================================

# ==========================================
# 0. 初始化 SQLite 数据库
# ==========================================
DB_FILE = "private_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS resources (
            short_code TEXT PRIMARY KEY,
            file_list TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_save_resource(short_code, file_list):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO resources (short_code, file_list) VALUES (?, ?)", 
                   (short_code, json.dumps(file_list)))
    conn.commit()
    conn.close()

def db_get_resource(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT file_list FROM resources WHERE short_code = ?", (short_code,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return None

def db_get_all_resources():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT short_code, file_list, created_at FROM resources ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    results = []
    for row in rows:
        results.append({
            "short_code": row[0], 
            "count": len(json.loads(row[1])),
            "created_at": row[2]
        })
    return results

def db_delete_resource(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM resources WHERE short_code = ?", (short_code,))
    conn.commit()
    conn.close()

UPLOADING_SESSIONS = {}

# ==========================================
# 1. 维持 Render 端口的网页服务器
# ==========================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Private Bot is running!")
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

# ==========================================
# 2. 接收文件（仅限主人上传）
# ==========================================
async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    
    chat_id = message.chat_id
    user_id = message.from_user.id

    logging.info(f"收到来自用户 ID: {user_id} 的消息")

    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        await message.reply_text("⛔ 这是一个私人专属机器人，您没有权限使用。")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    else:
        return

    if chat_id not in UPLOADING_SESSIONS:
        UPLOADING_SESSIONS[chat_id] = {"files": [], "msg_id": None}

    UPLOADING_SESSIONS[chat_id]["files"].append({"type": file_type, "file_id": file_id})
    total_count = len(UPLOADING_SESSIONS[chat_id]["files"])

    text = (
        f"📥 **正在归档私密文件中...**\n\n"
        f"当前已累计暂存：**{total_count}** 个文件\n"
        f"💡 发送完毕后，请点击下方按钮完成打包入库。"
    )

    keyboard = [[InlineKeyboardButton("✅ 归档完毕，生成永久存档", callback_data="finish_group")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    current_msg_id = UPLOADING_SESSIONS[chat_id]["msg_id"]
    try:
        if current_msg_id is None:
            sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            UPLOADING_SESSIONS[chat_id]["msg_id"] = sent_msg.message_id
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=current_msg_id, text=text, reply_markup=reply_markup, parse_mode="Markdown"
            )
    except Exception:
        sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        UPLOADING_SESSIONS[chat_id]["msg_id"] = sent_msg.message_id

# ==========================================
# 3. 按钮回调
# ==========================================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "finish_group":
        session_data = UPLOADING_SESSIONS.get(chat_id, {"files": [], "msg_id": None})
        session_files = session_data["files"]

        if not session_files:
            await query.message.reply_text("❌ 机器人刚才可能触发了云端休眠重启，暂存的文件已被刷新。请重新发送一次图片或视频即可！")
            return

        short_code = session_files[0]["file_id"][-8:]
        db_save_resource(short_code, session_files)
        UPLOADING_SESSIONS[chat_id] = {"files": [], "msg_id": None}

        raw_link = f"https://t.me/{BOT_USERNAME}?start={short_code}"

        text = (
            f"🎉 **私密归档成功！**\n\n"
            f"📁 包含文件总数：{len(session_files)} 个\n"
            f"🔑 存档识别码：`{short_code}`\n\n"
            f"🔗 **提取链接**：\n"
            f"[点击直达提取文件]({raw_link})\n\n"
            f"🛡️ **数据库已自动备份至您的聊天窗口**"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔗 一键提取查看", url=raw_link)],
            [InlineKeyboardButton("📋 点击复制链接", copy_text=raw_link)],
            [InlineKeyboardButton("🛠️ 进入管理/复查后台", callback_data="goto_glwj")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

        try:
            if os.path.exists(DB_FILE):
                with open(DB_FILE, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption="📦 【云端数据库自动备份】妥善保存此文件，数据永不丢失。"
                    )
        except Exception as e:
            logging.error(f"发送数据库备份失败: {e}")

    elif data == "goto_glwj":
        await show_glwj_panel(query.message, edit=True)

    elif data.startswith("del_"):
        short_code = data.split("_")[1]
        db_delete_resource(short_code)
        await query.message.edit_text(f"🗑️ 已成功从数据库中彻底抹除识别码为 `{short_code}` 的存档。", parse_mode="Markdown")

    elif data.startswith("view_"):
        short_code = data.split("_")[1]
        file_list = db_get_resource(short_code)
        if file_list:
            count = len(file_list)
            raw_link = f"https://t.me/{BOT_USERNAME}?start={short_code}"
            await query.message.reply_text(
                f"📌 **存档复查详情**\n\n"
                f"• 识别码：`{short_code}`\n"
                f"• 文件数量：{count} 个\n"
                f"• 提取链接：\n`{raw_link}`",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("❌ 未找到该存档记录，可能已被删除。")

# ==========================================
# 4. 私人复查后台：/glwj 命令
# ==========================================
async def show_glwj_panel(message_obj, edit=False):
    resources = db_get_all_resources()

    if not resources:
        text = "📂 目前私密云盘中还没有任何存档记录。"
        if edit:
            await message_obj.edit_text(text)
        else:
            await message_obj.reply_text(text)
        return

    text = f"🛠️ **私人云盘存档复查后台**\n共找到 {len(resources)} 组历史存档：\n\n"
    keyboard = []

    for item in resources:
        code = item["short_code"]
        count = item["count"]
        time_str = item["created_at"][:16]
        keyboard.append([
            InlineKeyboardButton(f"📁 [{time_str}] 码:{code} ({count}个)", callback_data=f"view_{code}"),
            InlineKeyboardButton("❌ 删除", callback_data=f"del_{code}")
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    if edit:
        await message_obj.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def glwj_command(update: Update, context: ContextTypes.DEFAULT_TYPE
