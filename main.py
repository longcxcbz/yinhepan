AD_CONFIG_FILE = "ad_config.json"

DEFAULT_AD_CONFIG = {
    "enabled": False,
    "photo_id": "",
    "caption": "",
    "btn_text": "",
    "btn_url": ""
}

def load_ad_config():
    # 如果 ad_config.json 不存在，自动生成一份默认文件
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

# 兼容原有调用接口，统一走 ad_config.json
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