import os
import requests
from datetime import datetime
from pathlib import Path


def _load_env():
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()


def tg_enabled():
    return bool(BOT_TOKEN and CHAT_ID)


def send_message(text: str):
    if not tg_enabled():
        return False, "telegram disabled (no TG_BOT_TOKEN / TG_CHAT_ID)"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code == 200:
            return True, "ok"
        return False, f"http {r.status_code}: {r.text[:250]}"
    except Exception as e:
        return False, str(e)


def send_hourly_stats(rows, period_label: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = sum(int(x[1]) for x in rows) if rows else 0

    lines = [
        "📊 <b>Почасовая статистика замен SIM</b>",
        f"⏱ Период: <b>{period_label}</b>",
        f"✅ Всего успешных: <b>{total}</b>",
        ""
    ]

    if not rows:
        lines.append("Нет успешных замен за период.")
    else:
        for alias, cnt in rows:
            lines.append(f"• <b>{alias}</b>: <b>{cnt}</b>")

    lines += ["", f"🕒 Сформировано: {now}"]
    return send_message("\n".join(lines))