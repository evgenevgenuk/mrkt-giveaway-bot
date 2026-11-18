"""
Робить файл .env.render зі всіма змінними для Render (Environment -> Add from .env).
Бере локальну сесію mrkt_session.session і config.json.
УВАГА: .env.render = повний доступ до твого Telegram-акаунта. Нікому не давай, не коміть.
"""
import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

BASE = Path(__file__).resolve().parent


async def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    tg = TelegramClient(str(BASE / cfg.get("session_name", "mrkt_session")), cfg["api_id"], cfg["api_hash"])
    await tg.connect()
    if not await tg.is_user_authorized():
        raise SystemExit("Локальна сесія не залогінена — спершу запусти python bot.py і увійди.")
    s = StringSession.save(tg.session)
    await tg.disconnect()
    lines = [
        f"API_ID={cfg['api_id']}",
        f"API_HASH={cfg['api_hash']}",
        f"BOT_TOKEN={cfg.get('bot_token', '')}",
        f"OWNER_IDS={','.join(map(str, cfg.get('owner_ids', [])))}",
        f"TG_SESSION={s}",
        "PYTHON_VERSION=3.12.7",
    ]
    (BASE / ".env.render").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Готово: .env.render")


if __name__ == "__main__":
    asyncio.run(main())
