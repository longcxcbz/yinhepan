import logging
import math
import os
import random
import re
import secrets
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

# 填写允许上传归档及执行管理命令的 Telegram 数字 ID（可填多个，用逗号分隔；留空 set() 则不限制）
ADMIN_USER_IDS = {
    8790114086,
    987654321,
}

def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS

DB_FILE = "private_database.db"
AD_CONFIG_FILE = "ad_config.json"

PAGE_SIZE = 10          # 每页 10 个文件（20 张图分 2 页，80 个分 8 页）
MAX_VISIBLE_PAGES = 5   # 首批平铺显示的页码数量，超出折叠为 "6-8" 范围按钮

DEFAULT_AD_CONFIG = {
    "enabled": False,
    "ads": []
}

# 记录每个用户对 (short_code, mode) 已获取过的页码集合：{(chat_id, short_code, mode): {1, 2}}
FETCHED_PAGES = {}

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

def db_code_exists(short_code):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM resources WHERE short_code = ? LIMIT 1", (short_code,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def generate_unique_short_code(length=8):
    while True:
        code = secrets.token_hex((length + 1) // 2)[:length]
        if not db_code_exists(code):
            return code

def db_save_resource(short_code, file_list):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO resources (short_code, file_list) VALUES (?, ?)", (short_code, json.dumps(file_list)))
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

# --- 2. ad_config.json 多广告池读写 ---
def clean_ad_url(raw_url):
    if not raw_url:
        return ""
    md_match = re.search(r"\((https?://[^\s\)]+)\)", raw_url)
    if md_match:
        url = md_match.group(1)
    else:
        url = re.sub(r"[\[\]\(\)<>\"']", "", raw_url).strip()
    url = url.strip(".,;!?")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url

def normalize_ad_item(item):
    if not isinstance(item, dict):
        return None
    media_type = item.get("media_type") or ("photo" if item.get("photo_id") else ("video" if item.get("video_id") else "text"))
    photo_id = item.get("photo_id") or ""
    video_id = item.get("video_id") or ""
    caption = item.get("caption") or ""

    raw_buttons = item.get("buttons")
    buttons = []
    if isinstance(raw_buttons, list):
        for b in raw_buttons:
            if isinstance(b, dict):
                b_text = (b.get("text") or "").strip()
                b_url = clean_ad_url(b.get("url") or "")
                if b_text and b_url:
                    buttons.append({"text": b_text, "url": b_url})
    else:
        b_text = (item.get("btn_text") or "").strip()
        b_url = clean_ad_url(item.get("btn_url") or "")
        if b_text and b_url:
            buttons.append({"text": b_text, "url": b_url})

    if media_type == "photo" and not photo_id and not caption and not buttons:
        return None
    if media_type == "video" and not video_id and not caption and not buttons:
        return None
    if media_type == "text" and not caption and not buttons:
        return None

    return {
        "media_type": media_type,
        "photo_id": photo_id,
        "video_id": video_id,
        "caption": caption,
        "buttons": buttons
    }

def load_ad_config():
    if not os.path.exists(AD_CONFIG_FILE):
        save_ad_config(DEFAULT_AD_CONFIG)
        return dict(DEFAULT_AD_CONFIG)
    try:
        with open(AD_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_AD_CONFIG)

        enabled = bool(data.get("enabled", False))
        ads = []
        if isinstance(data.get("ads"), list):
            for entry in data["ads"]:
                norm = normalize_ad_item(entry)
                if norm:
                    ads.append(norm)
        else:
            single = normalize_ad_item(data)
            if single:
                ads.append(single)

        return {"enabled": enabled, "ads": ads}
    except Exception as e:
        logging.error(f"读取 {AD_CONFIG_FILE} 失败: {e}")
        return dict(DEFAULT_AD_CONFIG)

def save_ad_config(config_dict):
    enabled = bool(config_dict.get("enabled", False))
    ads = []
    if isinstance(config_dict.get("ads"), list):
        for entry in config_dict["ads"]:
            norm = normalize_ad_item(entry)
            if norm:
                ads.append(norm)
    payload = {"enabled": enabled, "ads": ads}
    with open(AD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

load_ad_config()

def get_ads_list():
    return load_ad_config().get("ads") or []

def add_ad_to_config(media_type="text", photo_id="", video_id="", caption="", buttons=None):
    cfg = load_ad_config()
    new_ad = normalize_ad_item({
        "media_type": media_type or "text",
        "photo_id": photo_id or "",
        "video_id": video_id or "",
        "caption": caption or "",
        "buttons": buttons or []
    })
    if not new_ad:
        return None
    ads = cfg.get("ads", [])
    ads.append(new_ad)
    save_ad_config({"enabled": True, "ads": ads})
    return new_ad

def update_ad_at_index(idx_zero_based, media_type="text", photo_id="", video_id="", caption="", buttons=None):
    cfg = load_ad_config()
    ads = cfg.get("ads") or []
    if idx_zero_based < 0 or idx_zero_based >= len(ads):
        return None
    new_ad = normalize_ad_item({
        "media_type": media_type or "text",
        "photo_id": photo_id or "",
        "video_id": video_id or "",
        "caption": caption or "",
        "buttons": buttons or []
    })
    if not new_ad:
        return None
    ads[idx_zero_based] = new_ad
    save_ad_config({"enabled": True, "ads": ads})
    return new_ad

def delete_ad_at_index(idx_zero_based):
    cfg = load_ad_config()
    ads = cfg.get("ads") or []
    if idx_zero_based < 0 or idx_zero_based >= len(ads):
        return False
    ads.pop(idx_zero_based)
    save_ad_config({"enabled": bool(ads), "ads": ads})
    return True

def pick_random_ad():
    cfg = load_ad_config()
    if not cfg.get("enabled"):
        return None
    ads = cfg.get("ads") or []
    if not ads:
        return None
    return random.choice(ads)

def db_delete_ad():
    save_ad_config(DEFAULT_AD_CONFIG)

UPLOADING_SESSIONS = {}
WAITING_AD_USERS = set()
EDITING_AD_INDEX = {}

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

# --- 3. 分页状态与页码按钮构建 ---
def get_filtered_files(file_list, mode):
    if mode == "photo":
        return [x for x in file_list if x["type"] == "photo"]
    elif mode == "video":
        return [x for x in file_list if x["type"] == "video"]
    return file_list

def get_mode_label(mode):
    return {"all": "全部文件", "photo": "仅看图片", "video": "仅看视频"}.get(mode, "全部文件")

def mark_page_fetched(chat_id, short_code, mode, page_num):
    key = (chat_id, short_code, mode)
    if key not in FETCHED_PAGES:
        FETCHED_PAGES[key] = set()
    FETCHED_PAGES[key].add(page_num)

def get_fetched_set(chat_id, short_code, mode):
    return FETCHED_PAGES.get((chat_id, short_code, mode), set())

def format_page_label(page_num, current_page, fetched_set):
    if page_num == current_page:
        return f"📍{page_num}"
    if page_num in fetched_set:
        return f"✅{page_num}"
    return f"{page_num}"

def build_page_keyboard(chat_id, short_code, mode, total_pages, current_page, range_start=1, expand_range=False):
    fetched_set = get_fetched_set(chat_id, short_code, mode)
    keyboard = []

    if total_pages <= MAX_VISIBLE_PAGES:
        row = [
            InlineKeyboardButton(
                format_page_label(p, current_page, fetched_set),
                callback_data=f"page|{mode}|{short_code}|{p}|0|0"
            )
            for p in range(1, total_pages + 1)
        ]
        keyboard.append(row)
    else:
        if not expand_range:
            first_row = [
                InlineKeyboardButton(
                    format_page_label(p, current_page, fetched_set),
                    callback_data=f"page|{mode}|{short_code}|{p}|0|0"
                )
                for p in range(1, MAX_VISIBLE_PAGES + 1)
            ]
            keyboard.append(first_row)

            rem_start = MAX_VISIBLE_PAGES + 1
            range_row = []
            while rem_start <= total_pages:
                rem_end = min(rem_start + MAX_VISIBLE_PAGES - 1, total_pages)
                base_label = f"{rem_start}-{rem_end}" if rem_start != rem_end else f"{rem_start}"
                range_pages = set(range(rem_start, rem_end + 1))
                if current_page in range_pages:
                    label = f"📍{base_label}"
                elif range_pages & fetched_set:
                    label = f"✅{base_label}"
                else:
                    label = base_label
                range_row.append(
                    InlineKeyboardButton(
                        label,
                        callback_data=f"range|{mode}|{short_code}|{current_page}|{rem_start}|{rem_end}"
                    )
                )
                rem_start = rem_end + 1
            keyboard.append(range_row)
        else:
            expanded_row = [
                InlineKeyboardButton(
                    format_page_label(p, current_page, fetched_set),
                    callback_data=f"page|{mode}|{short_code}|{p}|{range_start}|1"
                )
                for p in range(range_start, min(range_start + MAX_VISIBLE_PAGES, total_pages + 1))
            ]
            keyboard.append(expanded_row)
            keyboard.append([
                InlineKeyboardButton(
                    "↩️ 返回 1-5 页码",
                    callback_data=f"page|{mode}|{short_code}|{current_page}|1|0"
                )
            ])

    keyboard.append([
        InlineKeyboardButton("⬅️ 返回存档概览", callback_data=f"backoverview|{short_code}")
    ])
    return InlineKeyboardMarkup(keyboard)

async def send_page_with_bar(chat_id, context, file_list, short_code, mode, page_num, range_start=1, expand_range=False):
    filtered = get_filtered_files(file_list, mode)
    total_items = len(filtered)
    if total_items == 0:
        await context.bot.send_message(chat_id=chat_id, text=f"📂 该存档中没有「{get_mode_label(mode)}」内容。")
        return

    total_pages = math.ceil(total_items / PAGE_SIZE)
    if page_num < 1 or page_num > total_pages:
        await context.bot.send_message(chat_id=chat_id, text="❌ 页码超出范围。")
        return

    mark_page_fetched(chat_id, short_code, mode, page_num)

    start_idx = (page_num - 1) * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_items)
    chunk = filtered[start_idx:end_idx]

    media = [
        InputMediaPhoto(media=x["file_id"]) if x["type"] == "photo" 
        else InputMediaVideo(media=x["file_id"]) 
        for x in chunk
    ]
    try:
        await context.bot.send_media_group(chat_id=chat_id, media=media)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 下发出错: {e}")
        return

    fetched_set = get_fetched_set(chat_id, short_code, mode)
    fetched_str = "、".join(str(p) for p in sorted(fetched_set)) if fetched_set else "无"
    bar_text = (
        f"📄 **{get_mode_label(mode)}** · 当前第 **{page_num}/{total_pages}** 页（第 {start_idx + 1}～{end_idx} 个，共 {total_items} 个）\n"
        f"📍 当前页：`📍{page_num}` ｜ ✅ 已获取：{fetched_str}\n"
        f"👇 点击下方页码切换或继续获取："
    )
    markup = build_page_keyboard(
        chat_id, short_code, mode, total_pages,
        current_page=page_num,
        range_start=range_start,
        expand_range=expand_range
    )
    await context.bot.send_message(chat_id=chat_id, text=bar_text, reply_markup=markup, parse_mode="Markdown")

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
        f"📦 总文件数：**{total_count}** 个（全部共 **{total_pages}** 页，每页 {PAGE_SIZE} 个）\n"
        f"💾 估计总容量：**{size_str}**\n\n"
        f"👇 **点击下方按钮直接获取第 1 页内容：**"
    )

    keyboard = [
        [InlineKeyboardButton(f"📑 全部第 1 页 ({total_count}个 / {total_pages}页)", callback_data=f"page|all|{short_code}|1|0|0")],
        [
            InlineKeyboardButton(f"🖼️ 图片第 1 页 ({photo_count}个)", callback_data=f"page|photo|{short_code}|1|0|0"),
            InlineKeyboardButton(f"🎬 视频第 1 页 ({video_count}个)", callback_data=f"page|video|{short_code}|1|0|0")
        ]
    ]
    return overview_text, InlineKeyboardMarkup(keyboard)

# --- 4. 多广告展示、管理与多按钮解析 ---
def build_ad_keyboard(buttons):
    if not buttons:
        return None
    rows = []
    for i in range(0, len(buttons), 2):
        row = [
            InlineKeyboardButton(btn["text"], url=btn["url"])
            for btn in buttons[i:i + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

async def send_specific_ad(chat_id, context, ad, is_preview=False):
    if not ad:
        if is_preview:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 当前没有可用的广告配置。")
        return

    reply_markup = build_ad_keyboard(ad.get("buttons") or [])
    try:
        mtype = ad["media_type"]
        if mtype == "video" and ad.get("video_id"):
            await context.bot.send_video(
                chat_id=chat_id,
                video=ad["video_id"],
                caption=ad.get("caption") or None,
                reply_markup=reply_markup
            )
        elif mtype == "photo" and ad.get("photo_id"):
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=ad["photo_id"],
                caption=ad.get("caption") or None,
                reply_markup=reply_markup
            )
        else:
            text_to_send = ad.get("caption") or (ad["buttons"][0]["url"] if ad.get("buttons") else "")
            if text_to_send:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text_to_send,
                    reply_markup=reply_markup,
                    disable_web_page_preview=False
                )
    except Exception as e:
        logging.error(f"发送广告失败: {e}")
        if is_preview:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **广告预览发送失败**\n原因：`{e}`\n请检查各按钮链接是否为有效网址。",
                parse_mode="Markdown"
            )

async def send_ad_to_user(chat_id, context, is_preview=False):
    ad = pick_random_ad()
    await send_specific_ad(chat_id, context, ad, is_preview=is_preview)

def _extract_buttons_from_line(line):
    parts = [p.strip() for p in re.split(r"[；;]", line) if p.strip()]
    parsed = []
    for part in parts:
        if "|" in part and ("http://" in part or "https://" in part or "t.me/" in part):
            b_text, b_url = part.split("|", 1)
            b_text = b_text.strip()
            b_url = clean_ad_url(b_url.strip())
            if b_text and b_url:
                parsed.append({"text": b_text, "url": b_url})
    return parsed

def parse_ad_text(raw_text):
    if not raw_text:
        return "", []
    lines = [line.rstrip() for line in raw_text.strip().split("\n") if line.strip()]
    if not lines:
        return "", []

    collected_buttons = []
    split_idx = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        line_btns = _extract_buttons_from_line(lines[idx])
        if line_btns:
            collected_buttons.extend(reversed(line_btns))
            split_idx = idx
        else:
            break

    collected_buttons.reverse()
    caption = "\n".join(lines[:split_idx]).strip()
    return caption, collected_buttons

def build_ad_list_payload():
    cfg = load_ad_config()
    ads = cfg.get("ads") or []
    if not ads:
        return "📭 当前广告池为空（0 条广告）。发送 `/setad` 可添加。", None

    lines = [f"📢 **广告池列表**（状态：{'已启用' if cfg.get('enabled') else '已停用'}，共 **{len(ads)}** 条，提取时随机展示）：\n"]
    keyboard = []
    for i, ad in enumerate(ads, start=1):
        mtype_cn = {"photo": "图片", "video": "视频", "text": "纯文本"}.get(ad["media_type"], "文本")
        preview_txt = (ad.get("caption") or "").replace("\n", " ").strip()
        if not preview_txt:
            preview_txt = ad["buttons"][0]["url"] if ad.get("buttons") else "无文案"
        if len(preview_txt) > 18:
            preview_txt = preview_txt[:18] + "..."
        btn_cnt = len(ad.get("buttons") or [])
        lines.append(f"**#{i}** `[{mtype_cn}]` {preview_txt}（{btn_cnt}按钮）")
        keyboard.append([
            InlineKeyboardButton(f"👀 预览 #{i}", callback_data=f"adprev|{i}"),
            InlineKeyboardButton(f"🗑️ 删除 #{i}", callback_data=f"addel|{i}")
        ])

    lines.append("\n💡 修改某条请发 `/editad 序号`（如 `/editad 1`）；删除某条可点按钮或发 `/delad 序号`；清空全部发 `/delad`。")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)

async def listad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text, markup = build_ad_list_payload()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def editad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    ads = get_ads_list()
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"⚠️ 请指定广告序号，例如 `/editad 1`（当前共 {len(ads)} 条）。", parse_mode="Markdown")
        return
    seq = int(context.args[0])
    if seq < 1 or seq > len(ads):
        await update.message.reply_text(f"❌ 序号 `{seq}` 不存在，当前有效范围为 `1`～`{len(ads)}`。", parse_mode="Markdown")
        return

    WAITING_AD_USERS.discard(user_id)
    EDITING_AD_INDEX[user_id] = seq - 1
    await update.message.reply_text(
        f"✏️ **正在修改第 #{seq} 条广告**\n请发送新的纯文本、带说明的图片或视频（末尾可附带按钮），将直接替换第 #{seq} 条。\n发送 `/cancelad` 可取消。",
        parse_mode="Markdown"
    )

async def setad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    EDITING_AD_INDEX.pop(user_id, None)
    WAITING_AD_USERS.add(user_id)
    guide_text = (
        "📢 **进入广告添加模式（支持多广告随机 + 多按钮）**\n\n"
        "请发送**纯文本**、**带说明的图片**或**带说明的视频**，末尾可写一个或多个按钮（支持多行或用 `；` 分隔）：\n\n"
        "```\n"
        "精选资源每日更新，欢迎加入！\n"
        "主频道|[https://t.me/jisogd](https://t.me/jisogd)；备用群|[https://t.me/your_group](https://t.me/your_group)\n"
        "```\n"
        "💡 **说明**：\n"
        "• 每次添加会自动分配序号（`#1`, `#2`...）并写入 `ad_config.json`。\n"
        "• 发送 `/listad` 查看序号、`/editad 序号` 修改单条、`/delad 序号` 删除单条、`/delad` 清空全部、`/cancelad` 取消。"
    )
    await update.message.reply_text(guide_text, parse_mode="Markdown")

async def cancelad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    was_waiting = user_id in WAITING_AD_USERS
    was_editing = user_id in EDITING_AD_INDEX
    WAITING_AD_USERS.discard(user_id)
    EDITING_AD_INDEX.pop(user_id, None)
    if was_waiting or was_editing:
        await update.message.reply_text("❎ 已退出广告设置/修改模式。")

async def delad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ads = get_ads_list()
    if context.args:
        if not context.args[0].isdigit():
            await update.message.reply_text("⚠️ 序号须为数字，例如 `/delad 2`；不带数字 `/delad` 则清空全部广告。", parse_mode="Markdown")
            return
        seq = int(context.args[0])
        if seq < 1 or seq > len(ads):
            await update.message.reply_text(f"❌ 序号 `{seq}` 不存在，当前有效范围为 `1`～`{len(ads)}`。", parse_mode="Markdown")
            return
        if delete_ad_at_index(seq - 1):
            await update.message.reply_text(f"🗑️ 已删除第 **#{seq}** 条广告并更新 `ad_config.json`。", parse_mode="Markdown")
        return

    db_delete_ad()
    await update.message.reply_text("🗑️ 已清空 `ad_config.json` 中的全部广告。")

async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    user_id = message.from_user.id
    if user_id in EDITING_AD_INDEX:
        idx = EDITING_AD_INDEX.pop(user_id)
        caption, buttons = parse_ad_text(message.text)
        saved_ad = update_ad_at_index(idx, media_type="text", caption=caption, buttons=buttons)
        if saved_ad:
            await message.reply_text(f"✅ **已更新第 #{idx + 1} 条广告（纯文本）！** 预览如下：")
            await send_specific_ad(message.chat_id, context, saved_ad, is_preview=True)
        else:
            await message.reply_text("❌ 更新失败：广告内容为空或序号已失效。")
        return

    if user_id in WAITING_AD_USERS:
        WAITING_AD_USERS.remove(user_id)
        caption, buttons = parse_ad_text(message.text)
        saved_ad = add_ad_to_config(media_type="text", caption=caption, buttons=buttons)
        total_ads = len(get_ads_list())
        await message.reply_text(f"✅ **纯文本广告已加入广告池（编号 #{total_ads}，共 {total_ads} 条）！** 预览如下：")
        await send_specific_ad(message.chat_id, context, saved_ad, is_preview=True)

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
        cfg = load_ad_config()
        await message.reply_text(f"✅ **广告配置文件 `ad_config.json` 已加载生效（共 {len(cfg.get('ads', []))} 条广告）！** 随机预览如下：")
        await send_ad_to_user(message.chat_id, context, is_preview=True)
    else:
        await message.reply_text("📂 收到您的文件，但该文件不是 `.db` 数据库或 `ad_config.json` 配置文件。")

async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    chat_id = message.chat_id
    user_id = message.from_user.id

    if not is_admin(user_id):
        return

    if user_id in EDITING_AD_INDEX and (message.photo or message.video):
        idx = EDITING_AD_INDEX.pop(user_id)
        caption, buttons = parse_ad_text(message.caption or "")
        if message.video:
            saved_ad = update_ad_at_index(idx, media_type="video", video_id=message.video.file_id, caption=caption, buttons=buttons)
            label = "视频广告"
        else:
            saved_ad = update_ad_at_index(idx, media_type="photo", photo_id=message.photo[-1].file_id, caption=caption, buttons=buttons)
            label = "图文广告"
        if saved_ad:
            await message.reply_text(f"✅ **已更新第 #{idx + 1} 条广告（{label}）！** 预览如下：")
            await send_specific_ad(chat_id, context, saved_ad, is_preview=True)
        else:
            await message.reply_text("❌ 更新失败：序号已失效。")
        return

    if user_id in WAITING_AD_USERS and (message.photo or message.video):
        WAITING_AD_USERS.remove(user_id)
        caption, buttons = parse_ad_text(message.caption or "")
        if message.video:
            video_id = message.video.file_id
            saved_ad = add_ad_to_config(media_type="video", video_id=video_id, caption=caption, buttons=buttons)
            label = "视频广告"
        else:
            photo_id = message.photo[-1].file_id
            saved_ad = add_ad_to_config(media_type="photo", photo_id=photo_id, caption=caption, buttons=buttons)
            label = "图文广告"
        total_ads = len(get_ads_list())
        await message.reply_text(f"✅ **{label}已加入广告池（编号 #{total_ads}，共 {total_ads} 条）！** 预览如下：")
        await send_specific_ad(chat_id, context, saved_ad, is_preview=True)
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
    user_id = query.from_user.id
    data = query.data

    if data.startswith("adprev|"):
        if not is_admin(user_id):
            return
        seq = int(data.split("|")[1])
        ads = get_ads_list()
        if 1 <= seq <= len(ads):
            await context.bot.send_message(chat_id=chat_id, text=f"👀 **预览第 #{seq} 条广告：**", parse_mode="Markdown")
            await send_specific_ad(chat_id, context, ads[seq - 1], is_preview=True)
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 该条广告已不存在。")
        return

    if data.startswith("addel|"):
        if not is_admin(user_id):
            return
        seq = int(data.split("|")[1])
        if delete_ad_at_index(seq - 1):
            text, markup = build_ad_list_payload()
            if markup:
                await query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
            else:
                await query.message.edit_text(text, parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ 该条广告已不存在。")
        return

    if data == "finish_group":
        session_data = UPLOADING_SESSIONS.get(chat_id, {"files": [], "msg_id": None})
        session_files = session_data["files"]

        if not session_files:
            await query.message.edit_text("✅ 该组文件已经成功打包过了。")
            return

        short_code = generate_unique_short_code(8)
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

    elif data.startswith("page|"):
        _, mode, code, page_s, range_start_s, expand_s = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.reply_text("❌ 存档已失效。")
            return
        await send_page_with_bar(
            chat_id, context, file_list, code, mode,
            page_num=int(page_s),
            range_start=int(range_start_s) or 1,
            expand_range=(expand_s == "1")
        )

    elif data.startswith("range|"):
        _, mode, code, cur_page_s, rem_start_s, rem_end_s = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.edit_text("❌ 存档已失效。")
            return
        filtered = get_filtered_files(file_list, mode)
        total_pages = math.ceil(len(filtered) / PAGE_SIZE)
        cur_page = int(cur_page_s)
        rem_start = int(rem_start_s)
        rem_end = int(rem_end_s)
        markup = build_page_keyboard(
            chat_id, code, mode, total_pages,
            current_page=cur_page,
            range_start=rem_start,
            expand_range=True
        )
        await query.message.edit_reply_markup(reply_markup=markup)

    elif data.startswith("backoverview|"):
        _, code = data.split("|")
        file_list = db_get_resource(code)
        if not file_list:
            await query.message.edit_text("❌ 存档已失效。")
            return
        overview_text, markup = build_overview_payload(code, file_list)
        await query.message.edit_text(overview_text, reply_markup=markup, parse_mode="Markdown")

async def glwj_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await show_glwj_panel(update.message, edit=False)

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_admin(update.effective_user.id):
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
                    caption="📢 【广告配置】这是您当前的 ad_config.json 多广告配置文件。"
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
            await send_ad_to_user(chat_id, context, is_preview=False)
        else:
            await update.message.reply_text("❌ 未找到该存档记录，可能已失效。")
    else:
        await update.message.reply_text(
            "🔐 私人云盘就绪。直接发送图片或视频开始归档。\n\n"
            "• `/glwj` - 管理已有存档\n"
            "• `/setad` - 添加广告到广告池（随机展示）\n"
            "• `/listad` - 查看广告列表与序号\n"
            "• `/editad 序号` - 修改指定序号的广告\n"
            "• `/delad [序号]` - 删除指定序号广告（不带序号则清空全部）\n"
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
    app.add_handler(CommandHandler("listad", listad_command))
    app.add_handler(CommandHandler("editad", editad_command))
    app.add_handler(CommandHandler("cancelad", cancelad_command))
    app.add_handler(CommandHandler("delad", delad_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_incoming_document))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_incoming_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()

if __name__ == "__main__":
    main()
