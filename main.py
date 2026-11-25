import logging
import math
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
AD_CONFIG_FILE = "ad_config.json"

PAGE_SIZE = 10          # 每页 10 个文件（80 个自动分 8 页，与 Telegram 媒体组上限一致）
MAX_VISIBLE_PAGES = 5   # 首批平铺显示的页码数量，超出折叠为 "6-8" 范围按钮

DEFAULT_AD_CONFIG = {
    "enabled": False,
    "photo_id": "",
    "caption": "",
    "btn_text": "",
    "btn_url": ""
}

# --- 1. 数据库初始化与存档读写 ---
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
    results = []
    for r in rows:
        flist = json.loads(r[1])
        results.append({"short_code": r[0], "count": len(flist), "created_at": r[2]})
    return results

def db_delete_resource(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM resources WHERE short_code = ?", (short_code,))
    conn.commit()
    conn.close()

# --- 2. ad_config.json 广告文件读写 ---
def load_ad_config():
    if not os.path.exists(AD_CONFIG_FILE):
        save_ad_config(DEFAULT_AD_CONFIG)
        return dict(DEFAULT_AD_CONFIG)
    try:
        with open(AD_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_AD_CONFIG)
        if isinstance(data, dict):
            merged.update(data)
        return merged
    except Exception as e:
        logging.error(f"读取 {AD_CONFIG_FILE} 失败: {e}")
        return dict(DEFAULT_AD_CONFIG)

def save_ad_config(config_dict):
    merged = dict(DEFAULT_AD_CONFIG)
    merged.update(config_dict)
    with open(AD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

# 启动时确保 ad_config.json 存在
load_ad_config()

def db_save_ad(photo_id, caption, btn_text, btn_url):
    save_ad_config({
        "enabled": True,
        "photo_id": photo_id or "",
        "caption": caption or "",
        "btn_text": btn_text or "",
        "btn_url": btn_url or ""
    })

def db_get_ad():
    cfg = load_ad_config()
    if not cfg.get("enabled"):
        return None
    if not cfg.get("photo_id") and not cfg.get("caption"):
        return None
    return {
        "photo_id": cfg.get("photo_id") or None,
        "caption": cfg.get("caption") or "",
        "btn_text": cfg.get("btn_text") or "",
        "btn_url": cfg.get("btn_url") or ""
    }

def db_delete_ad():
    save_ad_config({
        "enabled": False,
        "photo_id": "",
        "caption": "",
        "btn_text": "",
        "btn_url": ""
    })

UPLOADING_SESSIONS = {}
WAITING_AD_USERS = set()

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

# --- 3. 分页筛选与页码键盘生成（支持 1..5 + 6-8 折叠展开） ---
def get_filtered_files(file_list, mode):
    if mode == "photo":
        return [x for x in file_list if x["type"] == "photo"]
    elif mode == "video":
        return [x for x in file_list if x["type"] == "video"]
    return file_list

def get_mode_label(mode):
    return {"all": "全部文件", "photo": "仅看图片", "video": "仅看视频"}.get(mode, "全部文件")

def build_pagination_keyboard(short_code, mode, total_pages, range_start=1, expand_range=False):
    keyboard = []
    
    if total_pages <= MAX_VISIBLE_PAGES:
        row = [
            InlineKeyboardButton(f"{p}", callback_data=f"page|{mode}|{short_code}|{p}")
            for p in range(1, total_pages + 1)
        ]
        keyboard.append(row)
    else:
        if not expand_range:
            # 第一排：1, 2, 3, 4, 5
            first_row = [
                InlineKeyboardButton(f"{p}", callback_data=f"page|{mode}|{short_code}|{p}")
                for p in range(1, MAX_VISIBLE_PAGES + 1)
            ]
            keyboard.append(first_row)
            
            # 第二排：剩余页码折叠为 "6-8" 等范围按钮
            rem_start = MAX_VISIBLE_PAGES + 1
            range_row = []
            while rem_start <= total_pages:
                rem_end = min(rem_start + MAX_VISIBLE_PAGES - 1, total_pages)
                label = f"{rem_start}-{rem_end}" if rem_start != rem_end else f"{rem_start}"
                range_row.append(
                    InlineKeyboardButton(label, callback_data=f"range|{mode}|{short_code}|{rem_start}|{rem_end}")
                )
                rem_start = rem_end + 1
            keyboard.append(range_row)
        else:
            # 展开指定区间（如 6, 7, 8）并附带返回 1-5 按钮
            expanded_row = [
                InlineKeyboardButton(f"{p}", callback_data=f"page|{mode}|{short_code}|{p}")
                for p in range(range_start, min(range_start + MAX_VISIBLE_PAGES, total_pages + 1))
            ]
            keyboard.append(expanded_row)
            keyboard.append([
                InlineKeyboardButton("↩️ 返回 1-5 页码", callback_data=f"pagepanel|{mode}|{short_code}|1|0")
            ])

    keyboard.append([
        InlineKeyboardButton("⬅️ 返回存档概览", callback_data=f"backoverview|{short_code}")
    ])
    return InlineKeyboardMarkup(keyboard)

async def send_single_page(chat_id, context, file_list, mode, page_num):
    filtered = get_filtered_files(file_list, mode)
    total_items = len(filtered)
    if total_items == 0:
        await context.bot.send_message(chat_id=chat_id, text=f"📂 该存档中没有「{get_mode_label(mode)}」内容。")
        return

    total_pages = math.ceil(total_items / PAGE_SIZE)
    if page_num < 1 or page_num > total_pages:
        await context.bot.send_message(chat_id=chat_id, text="❌ 页码超出范围。")
        return

    start_idx = (page_num - 1) * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_items)
    chunk = filtered[start_idx:end_idx]

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📄 正在发送「{get_mode_label(mode)}」第 **{page_num}/{total_pages}** 页（第 {start_idx + 1}～{end_idx} 个，共 {total_items} 个）...",
        parse_mode="Markdown"
    )

    media = [
        InputMediaPhoto(media=x["file_id"]) if x["type"] == "photo" 
        else InputMediaVideo(media=x["file_id"]) 
        for x in chunk
    ]
    try:
        await context.bot.send_media_group(chat_id=chat_id, media=media)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 下发出错: {e}")

def build_overview_payload(short_code, file_list):
    total_count = len(file_list)
    photo_count = sum(1 for x in file_list if x["type"] == "photo")
    video_count = sum(1 for x in file_list if x["type"] == "video")
    total_pages = math.ceil(total_count / PAGE_SIZE)

    total_bytes = sum(x.get("size", 0) for x in file_list)
    if total_bytes > 1024 * 1024 * 1024:
        size_str = f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"
    else:
        size_str = f"{total_bytes / (1024 * 1024):.2f} MB"

    overview_text = (
        f"📊 **私密存档概览**\n\n"
        f"🔑 识别码：`{short_code}`\n"
        f"🖼️ 图片数量：**{photo_count}** 个\n"
        f"🎬 视频数量：**{video_count}** 个\n"
        f"📦 总文件数：**{total_count}** 个（共 **{total_pages}** 页，每页 {PAGE_SIZE} 个）\n"
        f"💾 估计总容量：**{size_str}**\n\n"
        f"👇 **请选择浏览方式并点击页码：**"
    )

    keyboard = [
        [InlineKeyboardButton(f"📑 全部文件选页 ({total_count}个 / {total_pages}页)", callback_data=f"pagepanel|all|{short_code}|1|0")],
        [
            InlineKeyboardButton(f"🖼️ 图片选页 ({photo_count}个)", callback_data=f"pagepanel|photo|{short_code}|1|0"),
            InlineKeyboardButton(f"🎬 视频选页 ({video_count}个)", callback_data=f"pagepanel|video|{short_code}|1|0")
        ]
    ]
    return overview_text, InlineKeyboardMarkup(keyboard)

# --- 4. 广告展示与解析 ---
async def send_ad_to_user(chat_id, context):
    ad = db_get_ad()
    if not ad:
        return
    
    reply_markup = None
    if ad["btn_text"] and ad["btn_url"]:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(ad["btn_text"], url=ad["btn_url"])]
        ])
    
    try:
        if ad["photo_id"]:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=ad["photo_id"],
                caption=ad["caption"] or None,
                reply_markup=reply_markup
            )
        elif ad["caption"]:
            await context.bot.send_message(
                chat_id=chat_id,
                text=ad["caption"],
                reply_markup=reply_markup,
                disable_web_page_preview=False
            )
    except Exception as e:
        logging.error(f"发送广告失败: {e}")

def parse_ad_text(raw_text):
    if not raw_text:
        return "", "", ""
    lines = raw_text.strip().split("\n")
    last_line = lines[-1].strip()
    if "|" in last_line and ("http://" in last_line or "https://" in last_line):
        parts = last_line.split("|", 1)
        btn_text = parts[0].strip()
        btn_url = parts[1].strip()
        caption = "\n".join(lines[:-1]).strip()
        return caption, btn_text, btn_url
    return raw_text.strip(), "", ""

async def setad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        return
    WAITING_AD_USERS.add(user_id)
    guide_text = (
        "📢 **进入广告设置模式**\n\n"
        "请直接发送一条**【带文字的图片】**（或纯文字消息），格式如下：\n\n"
        "```\n"
        "这里写你的广告推广文案...\n"
        "支持多行换行。\n"
        "点击加入频道|[https://t.me/你的频道](https://t.me/你的频道)\n"
        "```\n"
        "💡 **说明**：\n"
        "• 设置后会自动写入 `ad_config.json` 独立配置文件。\n"
        "• 最后一行用 `按钮名称|链接` 格式会自动生成底部跳转按钮。\n"
        "• 发送 `/cancelad` 可取消本次设置。"
    )
    await update.message.reply_text(guide_text, parse_mode="Markdown")

async def cancelad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in WAITING_AD_USERS:
        WAITING_AD_USERS.remove(user_id)
        await update.message.reply_text("❎ 已退出广告设置模式。")

async def delad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        return
    db_delete_ad()
    await update.message.reply_text("🗑️ 当前广告已关闭并清空 `ad_config.json`。")

async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    user_id = message.from_user.id
    if user_id in WAITING_AD_USERS:
        WAITING_AD_USERS.remove(user_id)
        caption, btn_text, btn_url = parse_ad_text(message.text)
        db_save_ad(None, caption, btn_text, btn_url)
        await message.reply_text("✅ **纯文字广告已保存至 `ad_config.json`！** 效果预览如下：")
        await send_ad_to_user(message.chat_id, context)

async def handle_incoming_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.document:
        return
    document = message.document
    if document.file_name and document.file_name.endswith(".db"):
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(DB_FILE)
        init_db()
        await message.reply_text("✅ **数据库恢复成功！**\n已成功加载您上传的数据库文件，所有历史存档已恢复在线。")
    elif document.file_name == AD_CONFIG_FILE:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(AD_CONFIG_FILE)
        await message.reply_text("✅ **广告配置文件 `ad_config.json` 已加载生效！** 效果预览如下：")
        await send_ad_to_user(message.chat_id, context)
    else:
        await message.reply_text("📂 收到您的文件，但该文件不是 `.db` 数据库或 `ad_config.json` 配置文件。")

async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    chat_id = message.chat_id
    user_id = message.from_user.id

    if ADMIN_USER_ID != 0 and user_id != ADMIN_USER_ID:
        return

    if user_id in WAITING_AD_USERS and message.photo:
        WAITING_AD_USERS.remove(user_id)
        photo_id = message.photo[-1].file_id
        caption, btn_text, btn_url = parse_ad_text(message.caption or "")
        db_save_ad(photo_id, caption, btn_text, btn_url)
        await message.reply_text("✅ **图文广告已保存至 `ad_config.json`！** 效果预览如下：")
        await send_ad_to_user(chat_id, context)
        return

    file_size = 0
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        file_size = message.photo[-1].file_size or 0
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
        file_size = message.video.file_size or 0
    else:
        return

    if chat_id not in UPLOADING_SESSIONS:
        UPLOADING_SESSIONS[chat_id] = {"files": [], "msg_id": None}

    UPLOADING_SESSIONS[chat_id]["files"].append({
        "type": file_type, 
        "file_id": file_id,
        "size": file_size
    })
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

async def show_glwj_panel(message_obj, edit=False):
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "finish_group":
        session_data = UPLOADING_SESSIONS.get(chat_id, {"files": [], "msg_id": None})
        session_files = session_data["files"]

        if not session_files:
            await query.message.edit_text("✅ 该组文件已经成功打包过了。")
            return

        short_code = session_files[0]["file_id"][-8:]
        db_save_resource(short_code, session_files)
        UPLOADING_SESSIONS[chat_id] = {"files": [], "msg_id": None}

        raw_link = f"https://t.me/{BOT_USERNAME}?start={short_code}"
        text = (
            f"🎉 **归档成功！**\n"
            f"🔑 识别码：`{short_code}`\n\n"
            f"🔗 **提取链接**（点击下方代码即可复制）：\n"
            f"`{raw_link}`"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔗 一键提取查看", url=raw_link)],
            [InlineKeyboardButton("🛠️ 进入管理后台", callback_data="goto_glwj")]
        ]
        
        try:
            await query.message.edit_text("✅ **本组文件已打包入库完成！**", reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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
            await query.message.reply_text(f"📌 存档 `{code}`\n总数：{len(file_list)}个\n链接：\n`{raw_link}`", parse_mode="Markdown")

    elif data.startswith("pagepanel|"):
        _, mode, code, start_s, expand_s = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.edit_text("❌ 存档已失效。")
            return
        filtered = get_filtered_files(file_list, mode)
        if not filtered:
            await query.message.edit_text(f"📂 该存档中没有「{get_mode_label(mode)}」内容。")
            return
        total_pages = math.ceil(len(filtered) / PAGE_SIZE)
        text = (
            f"📑 **选择页码（{get_mode_label(mode)}）**\n"
            f"🔑 识别码：`{code}`\n"
            f"📦 符合条件共 **{len(filtered)}** 个文件，分为 **{total_pages}** 页（每页 {PAGE_SIZE} 个）\n\n"
            f"👇 点击下方页码获取对应内容："
        )
        markup = build_pagination_keyboard(
            code, mode, total_pages,
            range_start=int(start_s),
            expand_range=(expand_s == "1")
        )
        await query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("range|"):
        _, mode, code, rem_start_s, rem_end_s = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.edit_text("❌ 存档已失效。")
            return
        filtered = get_filtered_files(file_list, mode)
        total_pages = math.ceil(len(filtered) / PAGE_SIZE)
        rem_start = int(rem_start_s)
        rem_end = int(rem_end_s)
        text = (
            f"📑 **选择页码（{get_mode_label(mode)} · 第 {rem_start}～{rem_end} 页）**\n"
            f"🔑 识别码：`{code}`\n\n"
            f"👇 点击下方具体页码获取文件："
        )
        markup = build_pagination_keyboard(
            code, mode, total_pages,
            range_start=rem_start,
            expand_range=True
        )
        await query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("backoverview|"):
        _, code = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.edit_text("❌ 存档已失效。")
            return
        overview_text, markup = build_overview_payload(code, file_list)
        await query.message.edit_text(overview_text, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("page|"):
        _, mode, code, page_s = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.reply_text("❌ 存档已失效。")
            return
        await send_single_page(chat_id, context, file_list, mode, int(page_s))

async def glwj_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_USER_ID != 0 and update.effective_user.id != ADMIN_USER_ID:
        return
    await show_glwj_panel(update.message, edit=False)

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if ADMIN_USER_ID != 0 and update.effective_user.id != ADMIN_USER_ID:
        return
    
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, 
                    document=f, 
                    caption="📦 【手动备份】这是您当前的数据库文件。"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ 发送备份文件失败: {e}")
    else:
        await update.message.reply_text("❌ 当前还没有生成数据库文件。")

    if os.path.exists(AD_CONFIG_FILE):
        try:
            with open(AD_CONFIG_FILE, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption="📢 【广告配置】这是您当前的 ad_config.json 广告文件。"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ 发送广告配置文件失败: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    chat_id = update.effective_chat.id
    if args:
        short_code = args[0]
        file_list = db_get_resource(short_code)
        if file_list:
            overview_text, reply_markup = build_overview_payload(short_code, file_list)
            await update.message.reply_text(overview_text, reply_markup=reply_markup, parse_mode="Markdown")
            await send_ad_to_user(chat_id, context)
        else:
            await update.message.reply_text("❌ 未找到该存档记录，可能已失效。")
    else:
        await update.message.reply_text(
            "🔐 私人云盘就绪。直接发送图片或视频开始归档。\n\n"
            "• `/glwj` - 管理已有存档\n"
            "• `/setad` - 设置提取时展示的图文广告（同步写入 ad_config.json）\n"
            "• `/delad` - 关闭并清空广告\n"
            "• `/backup` - 打包下载数据库与 ad_config.json"
        )

def main():
    token = os.environ.get("TOKEN")
    if not token:
        return
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("glwj", glwj_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("setad", setad_command))
    app.add_handler(CommandHandler("cancelad", cancelad_command))
    app.add_handler(CommandHandler("delad", delad_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_incoming_document))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_incoming_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()

if __name__ == "__main__":
    main()
