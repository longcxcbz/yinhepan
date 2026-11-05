import logging
import os
import sqlite3
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    InputMediaPhoto, 
    InputMediaVideo, 
    Update
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

BOT_USERNAME = "Yinhepan_bot"
ADMIN_USER_ID = 0
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
    cursor.execute("INSERT OR REPLACE INTO resources (short_code, file_list) VALUES (?, ?)", (short_code, json.dumps(file_list)))
    conn.commit()
    conn.close()

def db_get_resource(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT file_list FROM resources WHERE short_code = ?", (short_code,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def db_get_all_resources():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT short_code, file_list, created_at FROM resources ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"short_code": r[0], "count": len(json.loads(r[1])), "created_at": r[2]} for r in rows]

def db_delete_resource(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM resources WHERE short_code = ?", (short_code,))
    conn.commit()
    conn.close()

UPLOADING_SESSIONS = {}

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

async def handle_incoming_media(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.message
    if not message:
        return
    chat_id = message.chat_id
    user_id = message.from_user.id

    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
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

    text = f"📥 **正在归档...**\n已暂存：**{total_count}** 个文件"
    keyboard = [[InlineKeyboardButton("✅ 归档完毕，生成永久存档", callback_data="finish_group")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    current_msg_id = UPLOADING_SESSIONS[chat_id]["msg_id"]
    try:
        if current_msg_id is None:
            sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            UPLOADING_SESSIONS[chat_id]["msg_id"] = sent_msg.message_id
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, 
                message_id=current_msg_id, 
                text=text, 
                reply_markup=reply_markup, 
                parse_mode="Markdown"
            )
    except Exception:
        sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        UPLOADING_SESSIONS[chat_id]["msg_id"] = sent_msg.message_id

async def show_glwj_panel(
    message_obj, 
    edit=False
):
    resources = db_get_all_resources()
    if not resources:
        text = "📂 目前没有任何存档记录。"
        if edit:
            await message_obj.edit_text(text)
        else:
            await message_obj.reply_text(text)
        return

    text = f"🛠️ **存档复查后台**\n共找到 {len(resources)} 组存档："
    keyboard = [
        [
            InlineKeyboardButton(f"📁 码:{i['short_code']} ({i['count']}个)", callback_data=f"view_{i['short_code']}"), 
            InlineKeyboardButton("❌ 删除", callback_data=f"del_{i['short_code']}")
        ] 
        for i in resources
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if edit:
        await message_obj.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_callback(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "finish_group":
        session_data = UPLOADING_SESSIONS.get(chat_id, {"files": [], "msg_id": None})
        session_files = session_data["files"]

        if not session_files:
            await query.message.edit_text("✅ 该组文件已经成功打包过了，请直接使用上方生成的链接，或重新发送新文件进行归档。")
            return

        short_code = session_files[0]["file_id"][-8:]
        db_save_resource(short_code, session_files)
        UPLOADING_SESSIONS[chat_id] = {"files": [], "msg_id": None}

        raw_link = f"https://t.me/{BOT_USERNAME}?start={short_code}"
        text = f"🎉 **归档成功！**\n📁 文件数：{len(session_files)}\n🔑 识别码：`{short_code}`\n\n🔗 提取链接：\n[点击直达]({raw_link})"
        
        keyboard = [
            [InlineKeyboardButton("🔗 一键提取查看", url=raw_link)],
            [InlineKeyboardButton("📋 点击复制链接", copy_text=raw_link)],
            [InlineKeyboardButton("🛠️ 进入管理后台", callback_data="goto_glwj")]
        ]
        
        try:
            await query.message.edit_text("✅ **本组文件已打包入库完成！**", reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        try:
            if os.path.exists(DB_FILE):
                with open(DB_FILE, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption="📦 数据库自动备份")
        except Exception:
            pass

    elif data == "goto_glwj":
        await show_glwj_panel(query.message, edit=True)
    elif data.startswith("del_"):
        code = data.split("_")[1]
        db_delete_resource(code)
        await query.message.edit_text(f"🗑️ 已删除码 `{code}` 的存档。", parse_mode="Markdown")
    elif data.startswith("view_"):
        code = data.split("_")[1]
        file_list = db_get_resource(code)
        if file_list:
            raw_link = f"https://t.me/{BOT_USERNAME}?start={code}"
            await query.message.reply_text(f"📌 存档 `{code}`\n数量：{len(file_list)}个\n链接：\n`{raw_link}`", parse_mode="Markdown")

async def glwj_command(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE
):
    if ADMIN_USER_ID != 0 and update.effective_user.id != ADMIN_USER_ID:
        return
    await show_glwj_panel(update.message, edit=False)

async def start_command(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE
):
    args = context.args
    chat_id = update.effective_chat.id
    if args:
        file_list = db_get_resource(args[0])
        if file_list:
            await update.message.reply_text(f"🔍 正在下发共 {len(file_list)} 个文件...")
            for i in range(0, len(file_list), 10):
                chunk = file_list[i:i+10]
                media = [
                    InputMediaPhoto(media=x["file_id"]) if x["type"] == "photo" 
                    else InputMediaVideo(media=x["file_id"]) 
                    for x in chunk
                ]
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                except Exception as e:
                    await update.message.reply_text(f"❌ 出错: {e}")
        else:
            await update.message.reply_text("❌ 未找到该存档。")
    else:
        await update.message.reply_text("🔐 私人云盘就绪。直接发送图片或视频开始归档。")

def main():
    token = os.environ.get("TOKEN")
    if not token:
        return
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("glwj", glwj_command))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_incoming_media))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()

if __name__ == "__main__":
    main()
