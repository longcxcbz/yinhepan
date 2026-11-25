import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
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

# --- 两个机器人基础配置 ---
PAN_BOT_USERNAME = "Yinhepan_bot"
MANYUA_BOT_USERNAME = "Yinheximanyoua_bot"

MANYUA_DIR = "yinheximanyoua"
os.makedirs(MANYUA_DIR, exist_ok=True)

# @Yinhepan_bot 配置（原有）
PAN_DB_FILE = "private_database.db"
PAN_AD_CONFIG_FILE = "ad_config.json"

# @Yinheximanyoua_bot 配置（独立子目录与独立广告/数据库文件）
MANYUA_DB_FILE = os.path.join(MANYUA_DIR, "private_database.db")
MANYUA_AD_CONFIG_FILE = os.path.join(MANYUA_DIR, "ad_config.json")

# 填写 @Yinhepan_bot 管理员数字 ID（留空 set() 则不限制）
ADMIN_USER_IDS = {
    8790114086,
    970905815,
}

def is_pan_admin(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS

PAGE_SIZE = 10          # 每页 10 个文件；<= 10 直接下发，> 10 分页
MAX_VISIBLE_PAGES = 5   # 首批平铺显示的页码数量，超出折叠为 "6-8" 范围按钮

DEFAULT_AD_CONFIG = {
    "enabled": False,
    "ads": [],
}

# 按 bot_key ("pan" / "manyua") 隔离暂存与分页状态
UPLOADING_SESSIONS = {"pan": {}, "manyua": {}}
FETCHED_PAGES = {"pan": {}, "manyua": {}}
WAITING_AD_USERS = {"pan": set(), "manyua": set()}
EDITING_AD_INDEX = {"pan": {}, "manyua": {}}

# --- 1. 数据库读写（支持指定 db_file） ---
def init_db(db_file):
    conn = sqlite3.connect(db_file)
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

init_db(PAN_DB_FILE)
init_db(MANYUA_DB_FILE)

def db_code_exists(db_file, short_code):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM resources WHERE short_code = ? LIMIT 1", (short_code,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def generate_unique_short_code(db_file, length=8):
    while True:
        code = secrets.token_hex((length + 1) // 2)[:length]
        if not db_code_exists(db_file, code):
            return code

def db_save_resource(db_file, short_code, file_list):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO resources (short_code, file_list) VALUES (?, ?)", (short_code, json.dumps(file_list)))
    conn.commit()
    conn.close()

def db_get_resource(db_file, short_code):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT file_list FROM resources WHERE short_code = ?", (short_code,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def db_get_all_resources(db_file):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("SELECT short_code, file_list, created_at FROM resources ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    results = []
    for r in rows:
        flist = json.loads(r[1])
        results.append({"short_code": r[0], "count": len(flist), "created_at": r[2]})
    return results

def db_delete_resource(db_file, short_code):
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM resources WHERE short_code = ?", (short_code,))
    conn.commit()
    conn.close()

# --- 2. 广告文件读写（支持指定 ad_file） ---
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
        "buttons": buttons,
    }

def load_ad_config(ad_file):
    parent = os.path.dirname(ad_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(ad_file):
        save_ad_config(ad_file, DEFAULT_AD_CONFIG)
        return dict(DEFAULT_AD_CONFIG)
    try:
        with open(ad_file, "r", encoding="utf-8") as f:
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
        logging.error(f"读取 {ad_file} 失败: {e}")
        return dict(DEFAULT_AD_CONFIG)

def save_ad_config(ad_file, config_dict):
    parent = os.path.dirname(ad_file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    enabled = bool(config_dict.get("enabled", False))
    ads = []
    if isinstance(config_dict.get("ads"), list):
        for entry in config_dict["ads"]:
            norm = normalize_ad_item(entry)
            if norm:
                ads.append(norm)
    payload = {"enabled": enabled, "ads": ads}
    with open(ad_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

load_ad_config(PAN_AD_CONFIG_FILE)
load_ad_config(MANYUA_AD_CONFIG_FILE)

def get_ads_list(ad_file):
    return load_ad_config(ad_file).get("ads") or []

def add_ad_to_config(ad_file, media_type="text", photo_id="", video_id="", caption="", buttons=None):
    cfg = load_ad_config(ad_file)
    new_ad = normalize_ad_item({
        "media_type": media_type or "text",
        "photo_id": photo_id or "",
        "video_id": video_id or "",
        "caption": caption or "",
        "buttons": buttons or [],
    })
    if not new_ad:
        return None
    ads = cfg.get("ads", [])
    ads.append(new_ad)
    save_ad_config(ad_file, {"enabled": True, "ads": ads})
    return new_ad

def update_ad_at_index(ad_file, idx_zero_based, media_type="text", photo_id="", video_id="", caption="", buttons=None):
    cfg = load_ad_config(ad_file)
    ads = cfg.get("ads") or []
    if idx_zero_based < 0 or idx_zero_based >= len(ads):
        return None
    new_ad = normalize_ad_item({
        "media_type": media_type or "text",
        "photo_id": photo_id or "",
        "video_id": video_id or "",
        "caption": caption or "",
        "buttons": buttons or [],
    })
    if not new_ad:
        return None
    ads[idx_zero_based] = new_ad
    save_ad_config(ad_file, {"enabled": True, "ads": ads})
    return new_ad

def delete_ad_at_index(ad_file, idx_zero_based):
    cfg = load_ad_config(ad_file)
    ads = cfg.get("ads") or []
    if idx_zero_based < 0 or idx_zero_based >= len(ads):
        return False
    ads.pop(idx_zero_based)
    save_ad_config(ad_file, {"enabled": bool(ads), "ads": ads})
    return True

def pick_random_ad(ad_file):
    cfg = load_ad_config(ad_file)
    if not cfg.get("enabled"):
        return None
    ads = cfg.get("ads") or []
    if not ads:
        return None
    return random.choice(ads)

def db_delete_ad(ad_file):
    save_ad_config(ad_file, DEFAULT_AD_CONFIG)

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

# --- 3. 格式识别、分页与投递逻辑 ---
def is_zip_document(document):
    if not document:
        return False
    fname = (document.file_name or "").lower()
    mime = (document.mime_type or "").lower()
    return fname.endswith(".zip") or mime in {"application/zip", "application/x-zip-compressed"}

def get_present_types(file_list):
    present = []
    for t in ("photo", "video", "zip"):
        if any(x.get("type") == t for x in file_list):
            present.append(t)
    return present

def get_filtered_files(file_list, mode):
    if mode in ("photo", "video", "zip"):
        return [x for x in file_list if x.get("type") == mode]
    return file_list

def get_mode_label(mode):
    return {
        "all": "全部文件",
        "photo": "图片",
        "video": "视频",
        "zip": "压缩包",
    }.get(mode, "全部文件")

def mark_page_fetched(bot_key, chat_id, short_code, mode, page_num):
    store = FETCHED_PAGES[bot_key]
    key = (chat_id, short_code, mode)
    if key not in store:
        store[key] = set()
    store[key].add(page_num)

def get_fetched_set(bot_key, chat_id, short_code, mode):
    return FETCHED_PAGES[bot_key].get((chat_id, short_code, mode), set())

def format_page_label(page_num, current_page, fetched_set):
    if page_num == current_page:
        return f"📍{page_num}"
    if page_num in fetched_set:
        return f"✅{page_num}"
    return f"{page_num}"

def build_page_keyboard(bot_key, chat_id, short_code, mode, total_pages, current_page, range_start=1, expand_range=False, show_back_overview=True):
    fetched_set = get_fetched_set(bot_key, chat_id, short_code, mode)
    keyboard = []
    back_flag = "1" if show_back_overview else "0"

    if total_pages <= MAX_VISIBLE_PAGES:
        row = [
            InlineKeyboardButton(
                format_page_label(p, current_page, fetched_set),
                callback_data=f"page|{mode}|{short_code}|{p}|0|0|{back_flag}"
            )
            for p in range(1, total_pages + 1)
        ]
        keyboard.append(row)
    else:
        if not expand_range:
            first_row = [
                InlineKeyboardButton(
                    format_page_label(p, current_page, fetched_set),
                    callback_data=f"page|{mode}|{short_code}|{p}|0|0|{back_flag}"
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
                        callback_data=f"range|{mode}|{short_code}|{current_page}|{rem_start}|{rem_end}|{back_flag}"
                    )
                )
                rem_start = rem_end + 1
            keyboard.append(range_row)
        else:
            expanded_row = [
                InlineKeyboardButton(
                    format_page_label(p, current_page, fetched_set),
                    callback_data=f"page|{mode}|{short_code}|{p}|{range_start}|1|{back_flag}"
                )
                for p in range(range_start, min(range_start + MAX_VISIBLE_PAGES, total_pages + 1))
            ]
            keyboard.append(expanded_row)
            keyboard.append([
                InlineKeyboardButton(
                    "↩️ 返回 1-5 页码",
                    callback_data=f"page|{mode}|{short_code}|{current_page}|1|0|{back_flag}"
                )
            ])

    if show_back_overview:
        keyboard.append([
            InlineKeyboardButton("⬅️ 返回格式选择", callback_data=f"backoverview|{short_code}")
        ])
    return InlineKeyboardMarkup(keyboard)

async def send_items_batch(chat_id, context, items):
    media_group = []
    zip_items = []
    for x in items:
        if x.get("type") in ("photo", "video"):
            media_group.append(x)
        else:
            zip_items.append(x)

    if media_group:
        tg_media = [
            InputMediaPhoto(media=x["file_id"]) if x["type"] == "photo"
            else InputMediaVideo(media=x["file_id"])
            for x in media_group
        ]
        await context.bot.send_media_group(chat_id=chat_id, media=tg_media)

    for z in zip_items:
        await context.bot.send_document(
            chat_id=chat_id,
            document=z["file_id"],
            filename=z.get("file_name") or None,
        )

async def send_page_with_bar(bot_key, chat_id, context, file_list, short_code, mode, page_num, range_start=1, expand_range=False, show_back_overview=True):
    filtered = get_filtered_files(file_list, mode)
    total_items = len(filtered)
    if total_items == 0:
        await context.bot.send_message(chat_id=chat_id, text=f"📂 该存档中没有「{get_mode_label(mode)}」内容。")
        return

    total_pages = math.ceil(total_items / PAGE_SIZE)
    if page_num < 1 or page_num > total_pages:
        await context.bot.send_message(chat_id=chat_id, text="❌ 页码超出范围。")
        return

    mark_page_fetched(bot_key, chat_id, short_code, mode, page_num)

    start_idx = (page_num - 1) * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_items)
    chunk = filtered[start_idx:end_idx]

    try:
        await send_items_batch(chat_id, context, chunk)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ 下发出错: {e}")
        return

    if total_pages <= 1 and not show_back_overview:
        return

    fetched_set = get_fetched_set(bot_key, chat_id, short_code, mode)
    fetched_str = "、".join(str(p) for p in sorted(fetched_set)) if fetched_set else "无"
    bar_text = (
        f"📄 **{get_mode_label(mode)}** · 当前第 **{page_num}/{total_pages}** 页（第 {start_idx + 1}～{end_idx} 个，共 {total_items} 个）\n"
        f"📍 当前页：`📍{page_num}` ｜ ✅ 已获取：{fetched_str}\n"
        f"👇 点击下方页码切换或继续获取："
    )
    markup = build_page_keyboard(
        bot_key, chat_id, short_code, mode, total_pages,
        current_page=page_num,
        range_start=range_start,
        expand_range=expand_range,
        show_back_overview=show_back_overview,
    )
    await context.bot.send_message(chat_id=chat_id, text=bar_text, reply_markup=markup, parse_mode="Markdown")

def build_multi_type_selector_payload(short_code, file_list):
    total_count = len(file_list)
    photo_count = sum(1 for x in file_list if x.get("type") == "photo")
    video_count = sum(1 for x in file_list if x.get("type") == "video")
    zip_count = sum(1 for x in file_list if x.get("type") == "zip")
    total_pages = math.ceil(total_count / PAGE_SIZE)

    total_bytes = sum(x.get("size", 0) for x in file_list)
    if total_bytes > 1024 * 1024 * 1024:
        size_str = f"{total_bytes / (1024 * 1024 * 1024):.2f} GB"
    else:
        size_str = f"{total_bytes / (1024 * 1024):.2f} MB"

    breakdown_lines = []
    if photo_count:
        breakdown_lines.append(f"🖼️ 图片：**{photo_count}** 个")
    if video_count:
        breakdown_lines.append(f"🎬 视频：**{video_count}** 个")
    if zip_count:
        breakdown_lines.append(f"🗜️ 压缩包：**{zip_count}** 个")

    overview_text = (
        f"📊 **存档包含多种格式（共 {total_count} 个）**\n\n"
        f"🔑 识别码：`{short_code}`\n"
        + "\n".join(breakdown_lines) + "\n"
        f"💾 估计总容量：**{size_str}**\n\n"
        f"👇 **请选择要查看的格式（直接下发第 1 页）：**"
    )

    keyboard = [
        [InlineKeyboardButton(f"📑 全部第 1 页 ({total_count}个 / {total_pages}页)", callback_data=f"page|all|{short_code}|1|0|0|1")]
    ]
    type_buttons = []
    if photo_count:
        type_buttons.append(InlineKeyboardButton(f"🖼️ 图片 ({photo_count})", callback_data=f"page|photo|{short_code}|1|0|0|1"))
    if video_count:
        type_buttons.append(InlineKeyboardButton(f"🎬 视频 ({video_count})", callback_data=f"page|video|{short_code}|1|0|0|1"))
    if zip_count:
        type_buttons.append(InlineKeyboardButton(f"🗜️ 压缩包 ({zip_count})", callback_data=f"page|zip|{short_code}|1|0|0|1"))

    for i in range(0, len(type_buttons), 2):
        keyboard.append(type_buttons[i:i + 2])

    return overview_text, InlineKeyboardMarkup(keyboard)

async def deliver_pan_resource(chat_id, context, short_code, file_list):
    total_count = len(file_list)
    if total_count == 0:
        await context.bot.send_message(chat_id=chat_id, text="📂 该存档为空。")
        return

    if total_count <= PAGE_SIZE:
        try:
            await send_items_batch(chat_id, context, file_list)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ 下发出错: {e}")
        return

    present_types = get_present_types(file_list)
    if len(present_types) == 1:
        await send_page_with_bar(
            "pan", chat_id, context, file_list, short_code,
            mode=present_types[0], page_num=1,
            range_start=1, expand_range=False,
            show_back_overview=False
        )
        return

    overview_text, markup = build_multi_type_selector_payload(short_code, file_list)
    await context.bot.send_message(chat_id=chat_id, text=overview_text, reply_markup=markup, parse_mode="Markdown")

async def deliver_manyua_resource(chat_id, context, short_code, file_list):
    """
    @Yinheximanyoua_bot 提取规则：
    - 无需分类选择，直接按全部文件下发
    - <= 10 个：直接全部下发
    - > 10 个：直接下发第 1 页并附带与 @Yinhepan_bot 一样的页码栏，由用户点击页码切换
    """
    total_count = len(file_list)
    if total_count == 0:
        await context.bot.send_message(chat_id=chat_id, text="📂 该存档为空。")
        return

    if total_count <= PAGE_SIZE:
        try:
            await send_items_batch(chat_id, context, file_list)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ 下发出错: {e}")
        return

    await send_page_with_bar(
        "manyua", chat_id, context, file_list, short_code,
        mode="all", page_num=1,
        range_start=1, expand_range=False,
        show_back_overview=False
    )

# --- 4. 广告展示、管理与解析 ---
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
                reply_markup=reply_markup,
            )
        elif mtype == "photo" and ad.get("photo_id"):
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=ad["photo_id"],
                caption=ad.get("caption") or None,
                reply_markup=reply_markup,
            )
        else:
            text_to_send = ad.get("caption") or (ad["buttons"][0]["url"] if ad.get("buttons") else "")
            if text_to_send:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text_to_send,
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
    except Exception as e:
        logging.error(f"发送广告失败: {e}")
        if is_preview:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **广告预览发送失败**\n原因：`{e}`\n请检查各按钮链接是否为有效网址。",
                parse_mode="Markdown",
            )

async def send_ad_to_user(ad_file, chat_id, context, is_preview=False):
    ad = pick_random_ad(ad_file)
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

def build_ad_list_payload(ad_file):
    cfg = load_ad_config(ad_file)
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
            InlineKeyboardButton(f"🗑️ 删除 #{i}", callback_data=f"addel|{i}"),
        ])

    lines.append("\n💡 修改某条请发 `/editad 序号`；删除某条可点按钮或发 `/delad 序号`；清空全部发 `/delad`。")
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)

# --- 5. 构建单个 Bot 的处理器工厂 ---
def build_bot_handlers(bot_key, bot_username, db_file, ad_file, require_admin_for_upload):
    def can_manage(user_id):
        if bot_key == "pan":
            return is_pan_admin(user_id)
        # @Yinheximanyoua_bot 面向用户开放，若设置了 ADMIN_USER_IDS 则只限白名单管理广告；未设置则允许使用
        return True

    def can_upload(user_id):
        if require_admin_for_upload:
            return is_pan_admin(user_id)
        return True

    async def stage_file(message, chat_id, file_item):
        sessions = UPLOADING_SESSIONS[bot_key]
        if chat_id not in sessions:
            sessions[chat_id] = {"files": [], "msg_id": None}
        sessions[chat_id]["files"].append(file_item)
        total_count = len(sessions[chat_id]["files"])

        text = f"📥 **正在归档...**\n已暂存：**{total_count}** 个文件"
        keyboard = [[InlineKeyboardButton("✅ 确认数量，生成不重复识别码", callback_data="finish_group")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        current_msg_id = sessions[chat_id]["msg_id"]
        try:
            if current_msg_id is None:
                sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
                sessions[chat_id]["msg_id"] = sent_msg.message_id
            else:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=current_msg_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown",
                )
        except Exception:
            sent_msg = await message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            sessions[chat_id]["msg_id"] = sent_msg.message_id

    async def setad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not can_manage(user_id):
            return
        EDITING_AD_INDEX[bot_key].pop(user_id, None)
        WAITING_AD_USERS[bot_key].add(user_id)
        await update.message.reply_text(
            f"📢 **进入广告添加模式（独立文件：`{ad_file}`）**\n\n"
            "请发送纯文本、带说明的图片或视频，末尾可写一个或多个 `按钮名|链接`（支持多行或 `；` 分隔）：\n"
            "```\n"
            "精选资源每日更新，欢迎加入！\n"
            "主频道|[https://t.me/jisogd](https://t.me/jisogd)；备用群|[https://t.me/your_group](https://t.me/your_group)\n"
            "```\n"
            "发送 `/cancelad` 取消。",
            parse_mode="Markdown",
        )

    async def cancelad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        w = WAITING_AD_USERS[bot_key].discard(user_id)
        e = EDITING_AD_INDEX[bot_key].pop(user_id, None)
        if w is not None or e is not None:
            await update.message.reply_text("❎ 已退出广告设置/修改模式。")

    async def listad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not can_manage(update.effective_user.id):
            return
        text, markup = build_ad_list_payload(ad_file)
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

    async def editad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not can_manage(user_id):
            return
        ads = get_ads_list(ad_file)
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text(f"⚠️ 请指定序号，例如 `/editad 1`（当前共 {len(ads)} 条）。", parse_mode="Markdown")
            return
        seq = int(context.args[0])
        if seq < 1 or seq > len(ads):
            await update.message.reply_text(f"❌ 序号 `{seq}` 不存在，有效范围为 `1`～`{len(ads)}`。", parse_mode="Markdown")
            return
        WAITING_AD_USERS[bot_key].discard(user_id)
        EDITING_AD_INDEX[bot_key][user_id] = seq - 1
        await update.message.reply_text(
            f"✏️ **正在修改第 #{seq} 条广告**\n请发送新的纯文本、带说明的图片或视频（末尾可附带按钮）。发送 `/cancelad` 取消。",
            parse_mode="Markdown",
        )

    async def delad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not can_manage(update.effective_user.id):
            return
        ads = get_ads_list(ad_file)
        if context.args:
            if not context.args[0].isdigit():
                await update.message.reply_text("⚠️ 序号须为数字，例如 `/delad 2`；不带数字 `/delad` 清空全部。", parse_mode="Markdown")
                return
            seq = int(context.args[0])
            if seq < 1 or seq > len(ads):
                await update.message.reply_text(f"❌ 序号 `{seq}` 不存在，有效范围为 `1`～`{len(ads)}`。", parse_mode="Markdown")
                return
            if delete_ad_at_index(ad_file, seq - 1):
                await update.message.reply_text(f"🗑️ 已删除第 **#{seq}** 条广告。", parse_mode="Markdown")
            return
        db_delete_ad(ad_file)
        await update.message.reply_text("🗑️ 已清空全部广告。")

    async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        if not message or not message.text:
            return
        user_id = message.from_user.id
        if user_id in EDITING_AD_INDEX[bot_key]:
            idx = EDITING_AD_INDEX[bot_key].pop(user_id)
            caption, buttons = parse_ad_text(message.text)
            saved_ad = update_ad_at_index(ad_file, idx, media_type="text", caption=caption, buttons=buttons)
            if saved_ad:
                await message.reply_text(f"✅ **已更新第 #{idx + 1} 条广告（纯文本）！** 预览如下：")
                await send_specific_ad(message.chat_id, context, saved_ad, is_preview=True)
            else:
                await message.reply_text("❌ 更新失败：内容为空或序号已失效。")
            return

        if user_id in WAITING_AD_USERS[bot_key]:
            WAITING_AD_USERS[bot_key].remove(user_id)
            caption, buttons = parse_ad_text(message.text)
            saved_ad = add_ad_to_config(ad_file, media_type="text", caption=caption, buttons=buttons)
            total_ads = len(get_ads_list(ad_file))
            await message.reply_text(f"✅ **纯文本广告已加入广告池（编号 #{total_ads}，共 {total_ads} 条）！** 预览如下：")
            await send_specific_ad(message.chat_id, context, saved_ad, is_preview=True)

    async def handle_incoming_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        if not message or not message.document:
            return
        document = message.document
        chat_id = message.chat_id
        user_id = message.from_user.id

        if document.file_name and document.file_name.endswith(".db"):
            if not can_manage(user_id):
                return
            file = await context.bot.get_file(document.file_id)
            await file.download_to_drive(db_file)
            init_db(db_file)
            await message.reply_text("✅ **数据库恢复成功！**")
            return

        if document.file_name == os.path.basename(ad_file):
            if not can_manage(user_id):
                return
            file = await context.bot.get_file(document.file_id)
            await file.download_to_drive(ad_file)
            cfg = load_ad_config(ad_file)
            await message.reply_text(f"✅ **广告配置已加载生效（共 {len(cfg.get('ads', []))} 条）！** 随机预览如下：")
            await send_ad_to_user(ad_file, chat_id, context, is_preview=True)
            return

        if is_zip_document(document):
            if not can_upload(user_id):
                return
            await stage_file(message, chat_id, {
                "type": "zip",
                "file_id": document.file_id,
                "file_name": document.file_name or "archive.zip",
                "size": document.file_size or 0,
            })
            return

        if can_manage(user_id):
            await message.reply_text("📂 仅支持归档图片、视频与 `.zip` 压缩包（或上传 `.db` / 广告配置恢复）。")

    async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        if not message:
            return
        chat_id = message.chat_id
        user_id = message.from_user.id

        if user_id in EDITING_AD_INDEX[bot_key] and (message.photo or message.video):
            if not can_manage(user_id):
                return
            idx = EDITING_AD_INDEX[bot_key].pop(user_id)
            caption, buttons = parse_ad_text(message.caption or "")
            if message.video:
                saved_ad = update_ad_at_index(ad_file, idx, media_type="video", video_id=message.video.file_id, caption=caption, buttons=buttons)
                label = "视频广告"
            else:
                saved_ad = update_ad_at_index(ad_file, idx, media_type="photo", photo_id=message.photo[-1].file_id, caption=caption, buttons=buttons)
                label = "图文广告"
            if saved_ad:
                await message.reply_text(f"✅ **已更新第 #{idx + 1} 条广告（{label}）！** 预览如下：")
                await send_specific_ad(chat_id, context, saved_ad, is_preview=True)
            else:
                await message.reply_text("❌ 更新失败：序号已失效。")
            return

        if user_id in WAITING_AD_USERS[bot_key] and (message.photo or message.video):
            if not can_manage(user_id):
                return
            WAITING_AD_USERS[bot_key].remove(user_id)
            caption, buttons = parse_ad_text(message.caption or "")
            if message.video:
                saved_ad = add_ad_to_config(ad_file, media_type="video", video_id=message.video.file_id, caption=caption, buttons=buttons)
                label = "视频广告"
            else:
                saved_ad = add_ad_to_config(ad_file, media_type="photo", photo_id=message.photo[-1].file_id, caption=caption, buttons=buttons)
                label = "图文广告"
            total_ads = len(get_ads_list(ad_file))
            await message.reply_text(f"✅ **{label}已加入广告池（编号 #{total_ads}，共 {total_ads} 条）！** 预览如下：")
            await send_specific_ad(chat_id, context, saved_ad, is_preview=True)
            return

        if not can_upload(user_id):
            return

        if message.photo:
            await stage_file(message, chat_id, {
                "type": "photo",
                "file_id": message.photo[-1].file_id,
                "size": message.photo[-1].file_size or 0,
            })
        elif message.video:
            await stage_file(message, chat_id, {
                "type": "video",
                "file_id": message.video.file_id,
                "size": message.video.file_size or 0,
            })

    async def show_glwj_panel(message_obj, edit=False):
        resources = db_get_all_resources(db_file)
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
                InlineKeyboardButton("❌ 删除", callback_data=f"del_{i['short_code']}"),
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
            if not can_manage(user_id):
                return
            seq = int(data.split("|")[1])
            ads = get_ads_list(ad_file)
            if 1 <= seq <= len(ads):
                await context.bot.send_message(chat_id=chat_id, text=f"👀 **预览第 #{seq} 条广告：**", parse_mode="Markdown")
                await send_specific_ad(chat_id, context, ads[seq - 1], is_preview=True)
            else:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ 该条广告已不存在。")
            return

        if data.startswith("addel|"):
            if not can_manage(user_id):
                return
            seq = int(data.split("|")[1])
            if delete_ad_at_index(ad_file, seq - 1):
                text, markup = build_ad_list_payload(ad_file)
                if markup:
                    await query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
                else:
                    await query.message.edit_text(text, parse_mode="Markdown")
            else:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ 该条广告已不存在。")
            return

        if data == "finish_group":
            sessions = UPLOADING_SESSIONS[bot_key]
            session_data = sessions.get(chat_id, {"files": [], "msg_id": None})
            session_files = session_data["files"]

            if not session_files:
                await query.message.edit_text("✅ 该组文件已经成功打包过了。")
                return

            file_count = len(session_files)
            short_code = generate_unique_short_code(db_file, 8)
            db_save_resource(db_file, short_code, session_files)
            sessions[chat_id] = {"files": [], "msg_id": None}

            raw_link = f"https://t.me/{bot_username}?start={short_code}"
            text = (
                f"🎉 **归档成功！**\n"
                f"📦 已确认数量：**{file_count}** 个文件\n"
                f"🔑 识别码：`{short_code}`\n\n"
                f"🔗 **提取链接**（点击下方代码即可复制）：\n"
                f"`{raw_link}`"
            )
            keyboard = [
                [InlineKeyboardButton("🔗 一键提取查看", url=raw_link)],
                [InlineKeyboardButton("🛠️ 进入管理后台", callback_data="goto_glwj")],
            ]
            try:
                await query.message.edit_text(f"✅ **已确认 {file_count} 个文件并打包入库！**", reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        elif data == "goto_glwj":
            await show_glwj_panel(query.message, edit=True)
        elif data.startswith("del_"):
            code = data.split("_")[1]
            db_delete_resource(db_file, code)
            await query.message.edit_text(f"🗑️ 已删除码 `{code}` 的存档。", parse_mode="Markdown")
        elif data.startswith("view_"):
            code = data.split("_")[1]
            file_list = db_get_resource(db_file, code)
            if file_list:
                raw_link = f"https://t.me/{bot_username}?start={code}"
                await query.message.reply_text(f"📌 存档 `{code}`\n总数：{len(file_list)}个\n链接：\n`{raw_link}`", parse_mode="Markdown")

        elif data.startswith("page|"):
            parts = data.split("|")
            _, mode, code, page_s, range_start_s, expand_s = parts[:6]
            show_back = (parts[6] == "1") if len(parts) > 6 else False
            file_list = db_get_resource(db_file, code)
            if not file_list:
                await query.message.reply_text("❌ 存档已失效。")
                return
            effective_mode = "all" if bot_key == "manyua" else mode
            await send_page_with_bar(
                bot_key, chat_id, context, file_list, code, effective_mode,
                page_num=int(page_s),
                range_start=int(range_start_s) or 1,
                expand_range=(expand_s == "1"),
                show_back_overview=(show_back and bot_key == "pan"),
            )

        elif data.startswith("range|"):
            parts = data.split("|")
            _, mode, code, cur_page_s, rem_start_s, rem_end_s = parts[:6]
            show_back = (parts[6] == "1") if len(parts) > 6 else False
            file_list = db_get_resource(db_file, code)
            if not file_list:
                await query.message.edit_text("❌ 存档已失效。")
                return
            effective_mode = "all" if bot_key == "manyua" else mode
            filtered = get_filtered_files(file_list, effective_mode)
            total_pages = math.ceil(len(filtered) / PAGE_SIZE)
            markup = build_page_keyboard(
                bot_key, chat_id, code, effective_mode, total_pages,
                current_page=int(cur_page_s),
                range_start=int(rem_start_s),
                expand_range=True,
                show_back_overview=(show_back and bot_key == "pan"),
            )
            await query.message.edit_reply_markup(reply_markup=markup)

        elif data.startswith("backoverview|") and bot_key == "pan":
            _, code = data.split("|")
            file_list = db_get_resource(db_file, code)
            if not file_list:
                await query.message.edit_text("❌ 存档已失效。")
                return
            overview_text, markup = build_multi_type_selector_payload(code, file_list)
            await query.message.edit_text(overview_text, reply_markup=markup, parse_mode="Markdown")

    async def glwj_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not can_manage(update.effective_user.id):
            return
        await show_glwj_panel(update.message, edit=False)

    async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not can_manage(update.effective_user.id):
            return
        if os.path.exists(db_file):
            try:
                with open(db_file, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption="📦 【手动备份】数据库文件。")
            except Exception as e:
                await update.message.reply_text(f"❌ 发送数据库失败: {e}")
        if os.path.exists(ad_file):
            try:
                with open(ad_file, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption=f"📢 【广告配置】{ad_file}")
            except Exception as e:
                await update.message.reply_text(f"❌ 发送广告配置失败: {e}")

    async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        chat_id = update.effective_chat.id
        if args:
            short_code = args[0]
            file_list = db_get_resource(db_file, short_code)
            if file_list:
                # 提取时直接显示广告和文件
                await send_ad_to_user(ad_file, chat_id, context, is_preview=False)
                if bot_key == "manyua":
                    await deliver_manyua_resource(chat_id, context, short_code, file_list)
                else:
                    await deliver_pan_resource(chat_id, context, short_code, file_list)
            else:
                await update.message.reply_text("❌ 未找到该存档记录，可能已失效。")
        else:
            if bot_key == "manyua":
                await update.message.reply_text(
                    "✨ **@Yinheximanyoua_bot 已就绪**\n"
                    "直接发送图片、视频或 `.zip` 文件，确认数量后即可生成提取链接；超过 10 个文件将自动分页并由你点击页码查看。\n\n"
                    "• `/setad` - 添加独立广告\n"
                    "• `/listad` - 查看广告列表\n"
                    "• `/editad 序号` - 修改广告\n"
                    "• `/delad [序号]` - 删除广告\n"
                    "• `/glwj` - 管理存档",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "🔐 **@Yinhepan_bot 私人云盘就绪**\n"
                    "直接发送图片、视频或 `.zip` 开始归档。\n\n"
                    "• `/glwj` - 管理已有存档\n"
                    "• `/setad` - 添加广告到广告池（随机展示）\n"
                    "• `/listad` - 查看广告列表与序号\n"
                    "• `/editad 序号` - 修改指定序号的广告\n"
                    "• `/delad [序号]` - 删除指定序号广告（不带序号则清空全部）\n"
                    "• `/backup` - 打包下载数据库与 ad_config.json",
                    parse_mode="Markdown",
                )

    return [
        CommandHandler("start", start_command),
        CommandHandler("glwj", glwj_command),
        CommandHandler("backup", backup_command),
        CommandHandler("setad", setad_command),
        CommandHandler("listad", listad_command),
        CommandHandler("editad", editad_command),
        CommandHandler("cancelad", cancelad_command),
        CommandHandler("delad", delad_command),
        MessageHandler(filters.Document.ALL, handle_incoming_document),
        MessageHandler(filters.PHOTO | filters.VIDEO, handle_incoming_media),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_text),
        CallbackQueryHandler(button_callback),
    ]

def run_bot_in_thread(token, bot_key, bot_username, db_file, ad_file, require_admin_for_upload):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(token).build()
    for handler in build_bot_handlers(bot_key, bot_username, db_file, ad_file, require_admin_for_upload):
        app.add_handler(handler)
    app.run_polling(stop_signals=None)

def main():
    pan_token = os.environ.get("TOKEN")
    manyua_token = os.environ.get("MANYUA_TOKEN")

    if not pan_token and not manyua_token:
        return

    threads = []
    if pan_token:
        t_pan = threading.Thread(
            target=run_bot_in_thread,
            args=(pan_token, "pan", PAN_BOT_USERNAME, PAN_DB_FILE, PAN_AD_CONFIG_FILE, True),
            daemon=True,
        )
        t_pan.start()
        threads.append(t_pan)

    if manyua_token:
        t_manyua = threading.Thread(
            target=run_bot_in_thread,
            args=(manyua_token, "manyua", MANYUA_BOT_USERNAME, MANYUA_DB_FILE, MANYUA_AD_CONFIG_FILE, False),
            daemon=True,
        )
        t_manyua.start()
        threads.append(t_manyua)

    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
