"""
MRKT Free Giveaway Auto-Joiner
------------------------------
Юзербот (працює від ТВОГО Telegram-акаунта) який:
  1. логіниться в MRKT (@mrkt mini app) через initData,
  2. бере список АКТИВНИХ БЕЗПЛАТНИХ розіграшів,
  3. відбирає ті, де умова — тільки підписка на 1..N каналів
     (без бусту, без Premium, без "active trader", без квитків за TON),
  4. підписується на канали, проходить перевірку MRKT і бере участь,
  5. (опційно) відписується від каналів після завершення розіграшу.

Запуск:
  python bot.py            # працює по колу
  python bot.py --once     # один прохід
  python bot.py --dry      # тільки показати що б зробив (нічого не підписує і не бере участь)
"""

import argparse
import asyncio
import io
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from curl_cffi.requests import AsyncSession
from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.payments import GetStarGiftActiveAuctionsRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import (InputBotAppShortName, InputFolderPeer, InputNotifyPeer,
                               InputPeerNotifySettings)

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
API = "https://api.tgmrkt.io/api/v1"

log = logging.getLogger("mrkt")


# ----------------------------------------------------------------- config/state
DEFAULTS = {
    "api_id": 0,
    "api_hash": "",
    "session_name": "mrkt_session",
    "max_channels": 5,             # пропускати розіграші, де треба підписатись більше ніж на N каналів
    "check_interval_sec": 120,     # пауза між проходами
    "join_delay_sec": [15, 35],    # випадкова пауза між підписками (захист від FloodWait)
    "giveaway_delay_sec": [5, 15], # пауза між розіграшами
    "validation_timeout_sec": 90,  # скільки чекати поки MRKT підтвердить підписку
    "leave_after_end": True,       # відписатись від каналів, на які підписав бот, після кінця розіграшу
    "allow_premium_only": False,   # брати розіграші "тільки для Premium" (якщо в тебе є Premium)
    "skip_channels": [],           # канали, на які не підписуватись ніколи (без @)
    "proxy": None,                 # напр. "socks5://user:pass@host:port" (тільки для запитів до MRKT)
    "mute_and_archive": True,      # вимикати звук і кидати в архів канали, на які підписався бот
    "block_request_bots": True,    # блокувати ботів, що пишуть першими після заявки в канал
    "never_block": [],             # боти, яких ніколи не блокувати (без @)
    "owner_ids": [],               # хто керує ботом-пультом (заповнюється сам після /start)
    "bot_token": "",               # токен бота-пульта від @BotFather (сповіщення + команди), можна пусто
}


# ----------------------------------------------------------------- control bot
class Control:
    """Бот-пульт: шле сповіщення власнику і приймає команди. Без токена — просто нічого не робить."""

    def __init__(self):
        self.bot: TelegramClient | None = None
        self.bot_id: int | None = None
        self.owners: set[int] = set()
        self.paused = False
        self.wake = asyncio.Event()
        self.started = time.time()
        self.last_pass: str = "ще не було"
        self.stats = {"joined": 0, "won": 0, "errors": 0}
        self.blocked_until = 0.0     # MRKT віддає 403 Forbidden Section (блок за регіоном)
        self.blocked_notified = 0.0
        self.state: dict | None = None
        self.tg: TelegramClient | None = None

    async def notify(self, text: str) -> None:
        if not self.bot:
            return
        for uid in self.owners:
            try:
                await self.bot.send_message(uid, text, link_preview=False)
            except Exception as e:
                log.debug("notify %s failed: %s", uid, e)

    async def setup(self, cfg: dict, owner_id: int) -> None:
        if not cfg["bot_token"]:
            return
        self.owners = {owner_id} | {int(x) for x in cfg["owner_ids"]}
        bot_session = StringSession() if cfg["string_session"] else str(BASE / "control_bot")
        self.bot = TelegramClient(bot_session, cfg["api_id"], cfg["api_hash"])
        await self.bot.start(bot_token=cfg["bot_token"])
        me = await self.bot.get_me()
        self.bot_id = me.id

        @self.bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_cmd(ev):
            cmd = (ev.raw_text or "").split()[0].split("@")[0].lower() if ev.raw_text else ""
            if ev.sender_id not in self.owners:
                # перший, хто натисне /start, стає власником (якщо власника з AyuGram ще нема)
                if cmd == "/start" and not cfg["owner_ids"]:
                    cfg["owner_ids"].append(ev.sender_id)
                    self.owners.add(ev.sender_id)
                    save_owner_ids(cfg["owner_ids"])
                    log.info("Новий власник бота-пульта: %s", ev.sender_id)
                    await ev.reply("✅ Тепер ти власник цього бота.\n\n" + HELP)
                else:
                    await ev.reply("⛔ Це приватний бот.")
                return
            if cmd in ("/start", "/help"):
                await ev.reply(HELP)
            elif cmd == "/status":
                await ev.reply(self.status_text())
            elif cmd == "/pause":
                self.paused = True
                await ev.reply("⏸ Пауза. /resume — продовжити")
            elif cmd == "/resume":
                self.paused = False
                self.wake.set()
                await ev.reply("▶️ Працюю")
            elif cmd == "/run":
                self.wake.set()
                await ev.reply("🔄 Запускаю перевірку зараз")
            elif cmd == "/list":
                await ev.reply(self.list_text())
            elif cmd in ("/auction", "/auctions"):
                try:
                    await ev.reply(await auctions_text(self.tg), link_preview=False)
                except Exception as e:
                    await ev.reply(f"Не вдалось отримати аукціони: {e}")

        log.info("Бот-пульт: @%s (напиши йому /start, якщо ще не писав)", me.username)
        await self.notify("🤖 MRKT бот запущено\n\n" + HELP)

    def status_text(self) -> str:
        up = int(time.time() - self.started)
        active = sum(1 for v in (self.state or {}).get("joined", {}).values()
                     if parse_dt(v.get("endAt")) > time.time())
        return (f"{'⏸ ПАУЗА' if self.paused else '▶️ Працює'}\n"
                f"Аптайм: {up // 3600} год {up % 3600 // 60} хв\n"
                f"Остання перевірка: {self.last_pass}\n"
                f"За сесію: участь {self.stats['joined']}, перемог {self.stats['won']}, "
                f"помилок {self.stats['errors']}\n"
                f"Активних розіграшів з участю: {active}")

    def list_text(self) -> str:
        items = sorted((self.state or {}).get("joined", {}).items(),
                       key=lambda kv: kv[1].get("at", 0), reverse=True)[:15]
        lines = []
        for gid, v in items:
            chans = ", ".join("@" + c for c in v.get("channels", []))
            if v.get("won"):
                lines.append(f"🏆 ВИГРАВ — {chans}")
            elif parse_dt(v.get("endAt")) > time.time():
                lines.append(f"✅ участь прийнята — {chans} (кінець {fmt_dt(v.get('endAt'))})")
            else:
                lines.append(f"🏁 завершився, не виграв — {chans}")
        pend = (self.state or {}).get("pending", {})
        out = "Розіграші з прийнятою участю:\n" + ("\n".join(lines) if lines else "поки нема")
        if pend:
            out += "\n\n⏳ Чекають схвалення заявки в канал:\n" + "\n".join(
                f"@{p['channel']}" for p in pend.values())
        if CTL.blocked_until > time.time():
            out += "\n\n⛔ MRKT зараз блокує підтвердження участі з цього сервера (регіон)."
        return out


HELP = ("Команди:\n"
        "/status — стан\n"
        "/list — останні розіграші\n"
        "/auction — активні аукціони подарунків Telegram\n"
        "/run — перевірити зараз\n"
        "/pause — пауза\n"
        "/resume — продовжити")

CTL = Control()


def fmt_dt(s: str | None) -> str:
    ts = parse_dt(s)
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"


def save_owner_ids(ids: list) -> None:
    if not CONFIG_PATH.exists():  # сервер: config.json нема, власник живе в env OWNER_IDS
        log.info("Додай у змінні середовища OWNER_IDS=%s", ",".join(map(str, ids)))
        return
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["owner_ids"] = ids
    CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> dict:
    """config.json (локально) або змінні середовища (сервер: Render тощо)."""
    env_file = BASE / ".env"  # KEY=VALUE поруч з bot.py (для запуску на ПК)
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]  # KEY="value" (так експортує Render)
                os.environ.setdefault(k.strip(), v)
    raw =json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = {**DEFAULTS, **raw}
    env = os.environ
    if env.get("API_ID"):
        cfg["api_id"] = int(env["API_ID"])
    if env.get("API_HASH"):
        cfg["api_hash"] = env["API_HASH"]
    if env.get("BOT_TOKEN"):
        cfg["bot_token"] = env["BOT_TOKEN"]
    if env.get("OWNER_IDS"):
        cfg["owner_ids"] = [int(x) for x in env["OWNER_IDS"].replace(" ", "").split(",") if x]
    cfg["string_session"] = env.get("TG_SESSION", "").strip()
    # на сервері диск тимчасовий — стан бекапимо в «Збране» Telegram
    cfg["state_in_telegram"] = bool(cfg["string_session"]) or env.get("STATE_IN_TELEGRAM") == "1"
    if not CONFIG_PATH.exists() and not cfg["api_id"]:
        sys.exit("Нема config.json і змінних API_ID / API_HASH.")
    if not cfg["api_id"] or not cfg["api_hash"]:
        sys.exit("В config.json не заповнені api_id / api_hash (бери на https://my.telegram.org).")
    cfg["skip_channels"] = {c.lower().lstrip("@") for c in cfg["skip_channels"]}
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            log.warning("state.json пошкоджений — починаю з нуля")
    return {"joined": {}, "failed": {}, "to_leave": []}


class StateBackup:
    """Копія state.json у «Збраному» Telegram, щоб стан переживав перезапуски сервера."""
    TAG = "#mrkt_state"

    def __init__(self, tg: TelegramClient):
        self.tg = tg
        self.msg_id: int | None = None
        self.last = 0.0

    async def restore(self) -> dict | None:
        async for m in self.tg.iter_messages("me", search=self.TAG, limit=10):
            if m.file and self.TAG in (m.message or ""):
                self.msg_id = m.id
                data = await m.download_media(file=bytes)
                return json.loads(data.decode("utf-8"))
        return None

    async def push(self, state: dict, force: bool = False) -> None:
        if not force and time.time() - self.last < 300:
            return
        self.last = time.time()
        f = io.BytesIO(json.dumps(state, ensure_ascii=False).encode("utf-8"))
        f.name = "mrkt_state.json"
        caption = f"{self.TAG} (бекап MRKT бота, не видаляй) {datetime.now():%d.%m %H:%M}"
        try:
            m = await self.tg.send_file("me", f, caption=caption, silent=True)
            if self.msg_id:
                await self.tg.delete_messages("me", [self.msg_id])
            self.msg_id = m.id
        except Exception as e:
            log.debug("state backup: %s", e)


async def health_server(port: int) -> None:
    """Мінімальний HTTP — щоб Render бачив сервіс живим, а UptimeRobot міг його пінгувати."""
    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), 5)
        except Exception:
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n"
                     b"Connection: close\r\n\r\nok")
        try:
            await writer.drain()
        finally:
            writer.close()
    await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("Health-сервер на порту %s", port)


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def parse_dt(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def rsleep(rng) -> None:
    await asyncio.sleep(random.uniform(*rng))


# ----------------------------------------------------------------- MRKT API
class MrktError(Exception):
    pass


class MrktRegionBlocked(MrktError):
    """403 "Forbidden Section: Playhub" — MRKT не пускає в розіграші з IP цього сервера."""


class Mrkt:
    def __init__(self, tg: TelegramClient, proxy: str | None):
        self.tg = tg
        self.token: str | None = None
        self.http = AsyncSession(impersonate="chrome", proxy=proxy, timeout=30)

    def _headers(self) -> dict:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://cdn.tgmrkt.io",
            "Referer": "https://cdn.tgmrkt.io/",
        }
        if self.token:
            h["Authorization"] = self.token
            h["Cookie"] = f"access_token={self.token}"
        return h

    async def auth(self) -> None:
        peer = await self.tg.get_input_entity("mrkt")
        wv = await self.tg(RequestAppWebViewRequest(
            peer=peer,
            app=InputBotAppShortName(bot_id=peer, short_name="app"),
            platform="android",
        ))
        init_data = unquote(wv.url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])
        r = await self.http.post(f"{API}/auth", json={"data": init_data, "appId": None}, headers=self._headers())
        if r.status_code != 200 or not r.json().get("token"):
            raise MrktError(f"auth failed: {r.status_code} {r.text[:200]}")
        self.token = r.json()["token"]
        log.info("Залогінився в MRKT")

    async def req(self, method: str, path: str, *, params=None, json_body=None, _retry=True):
        if not self.token:
            await self.auth()
        r = await self.http.request(method, f"{API}{path}", params=params, json=json_body,
                                    headers=self._headers())
        if r.status_code == 401 and _retry:
            await self.auth()
            return await self.req(method, path, params=params, json_body=json_body, _retry=False)
        if r.status_code == 403 and "forbidden section" in r.text.lower():
            raise MrktRegionBlocked(f"{method} {path} -> 403 {r.text[:200]}")
        if r.status_code != 200:
            raise MrktError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {}

    async def free_giveaways(self) -> list[dict]:
        items, cursor = [], ""
        for _ in range(20):  # максимум 20 сторінок по 20
            data = await self.req("GET", "/giveaways", params={
                "count": 20, "cursor": cursor, "type": "Free",
                "isActive": "true", "ordering": "EndingTimeWithBadge",
            })
            items += data.get("items") or []
            cursor = data.get("nextCursor")
            if not cursor:
                break
        return items

    async def start_validation(self, gid: str, channel: str, vtype: str = "ChannelMember"):
        return await self.req("POST", f"/giveaways/start-validation/{gid}",
                              params={"channel": channel, "type": vtype})

    async def check_validations(self, gid: str) -> dict:
        return await self.req("GET", f"/giveaways/check-validations/{gid}")

    async def participate(self, gid: str):
        return await self.req("POST", f"/giveaways/buy-tickets/{gid}", params={"count": 1})


# ----------------------------------------------------------------- logic
def why_skip(g: dict, cfg: dict, state: dict) -> str | None:
    gid = g.get("id")
    chans = [c.lstrip("@") for c in (g.get("chanels") or g.get("channels") or [])]
    if gid in state["joined"]:
        return "вже беру участь"
    if g.get("myTicketsCount"):
        return "вже беру участь (MRKT)"
    if g.get("ticketPriceNanoTons"):
        return "платний"
    if g.get("validationStatus") != "Validated":
        return f"розіграш не підтверджений ({g.get('validationStatus')})"
    if parse_dt(g.get("endAt")) and parse_dt(g.get("endAt")) < time.time() + 30:
        return "закінчується"
    if g.get("isChanelBoostRequired"):
        return "потрібен буст"
    if g.get("isForActiveTraders"):
        return "тільки для active traders"
    if g.get("isForPremium") and not cfg["allow_premium_only"]:
        return "тільки для Premium"
    if not chans:
        return "нема каналів (не той тип умов)"
    if len(chans) > cfg["max_channels"]:
        return f"забагато каналів ({len(chans)})"
    if any(c.lower() in cfg["skip_channels"] for c in chans):
        return "канал у skip_channels"
    if state["failed"].get(gid, 0) >= 3:
        return "3 невдалі спроби"
    return None


async def mute_and_archive(tg: TelegramClient, ent, channel: str) -> None:
    """Вимкнути звук назавжди і перенести чат в архів."""
    try:
        peer = await tg.get_input_entity(ent)
        await tg(UpdateNotifySettingsRequest(
            peer=InputNotifyPeer(peer),
            settings=InputPeerNotifySettings(mute_until=2 ** 31 - 1, show_previews=False),
        ))
        await tg(EditPeerFoldersRequest([InputFolderPeer(peer, folder_id=1)]))
        log.info("    🔇 звук вимкнено, в архіві: @%s", channel)
    except errors.FloodWaitError as e:
        await asyncio.sleep(e.seconds + 2)
    except Exception as e:
        log.warning("    не вдалось замютити/архівувати @%s: %s", channel, e)


async def ensure_joined(tg: TelegramClient, channel: str, cfg: dict, state: dict,
                        gid: str = "", end_at: str | None = None):
    """True = щойно підписався, False = вже був підписаний, "pending" = заявка чекає схвалення,
    None = не вдалось."""
    try:
        ent = await tg.get_entity(channel)
    except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError):
        log.warning("  канал @%s не знайдено", channel)
        return None
    if getattr(ent, "left", True) is False:
        return False
    pending = state.setdefault("pending", {})
    if channel.lower() in pending:
        return "pending"  # заявку вже подали, повторно не спамимо
    while True:
        try:
            await tg(JoinChannelRequest(ent))
            log.info("  + підписався на @%s", channel)
            if cfg["mute_and_archive"]:
                await mute_and_archive(tg, ent, channel)
            await rsleep(cfg["join_delay_sec"])
            return True
        except errors.FloodWaitError as e:
            log.warning("  FloodWait %ss — чекаю", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except errors.ChannelsTooMuchError:
            log.error("  Ліміт каналів у Telegram! Відпишись від частини каналів.")
            await CTL.notify("⚠️ Ліміт каналів у Telegram (~500). Відпишись від частини каналів.")
            return None
        except errors.InviteRequestSentError:
            log.info("  ⏳ @%s — подав заявку, чекаю схвалення", channel)
            pending[channel.lower()] = {"channel": channel, "gid": gid, "endAt": end_at,
                                        "at": int(time.time())}
            state["last_request_at"] = int(time.time())
            save_state(state)
            await rsleep(cfg["join_delay_sec"])
            return "requested"
        except errors.RPCError as e:
            log.warning("  не вдалось підписатись на @%s: %s", channel, e)
            return None


async def validate(api: Mrkt, gid: str, chans: list[str], timeout: int) -> bool:
    for c in chans:
        await api.start_validation(gid, c)
        await asyncio.sleep(1)
    deadline = time.time() + timeout
    retried = set()
    while time.time() < deadline:
        await asyncio.sleep(5)
        v = await api.check_validations(gid)
        vals = {x.get("channel", "").lower().lstrip("@"): x for x in v.get("channelValidations") or []}
        ok = True
        for c in chans:
            st = (vals.get(c.lower()) or {}).get("isMember")
            if st == "Validated":
                continue
            ok = False
            if st in (None, "TimeOut", "NotValidated", "Failed") and c not in retried:
                retried.add(c)
                await api.start_validation(gid, c)
        if ok:
            return True
    return False


def track_joined(state: dict, channel: str, end_at: str | None, gid: str) -> None:
    for item in state["to_leave"]:
        if item.get("gid") == gid:
            if channel not in item["channels"]:
                item["channels"].append(channel)
            break
    else:
        state["to_leave"].append({"channels": [channel], "after": end_at, "gid": gid})
    save_state(state)


async def archive_tracked(tg, state: dict) -> None:
    """Один раз при старті: замютити/архівувати канали, на які бот підписався раніше."""
    done = set(state.setdefault("archived", []))
    for item in state["to_leave"]:
        for c in item["channels"]:
            if c.lower() in done:
                continue
            try:
                await mute_and_archive(tg, await tg.get_entity(c), c)
            except Exception as e:
                log.debug("archive @%s: %s", c, e)
            done.add(c.lower())
            await asyncio.sleep(1)
    state["archived"] = sorted(done)
    save_state(state)


async def handle_giveaway(tg, api: Mrkt, g: dict, cfg: dict, state: dict) -> None:
    gid = g["id"]
    chans = list(dict.fromkeys(c.lstrip("@") for c in (g.get("chanels") or g.get("channels"))))
    log.info("Розіграш %s | канали: %s | до %s", gid, ", ".join("@" + c for c in chans), g.get("endAt"))

    async def join_one(i: int, c: str):
        await asyncio.sleep(i * 1.5)  # невеликий зсув, щоб не ловити FloodWait
        return await ensure_joined(tg, c, cfg, state, gid, g.get("endAt"))

    # підписуємось на всі канали розіграшу одночасно
    results = await asyncio.gather(*(join_one(i, c) for i, c in enumerate(chans)))
    waiting, fresh_request = [], False
    for c, res in zip(chans, results):
        if res in ("pending", "requested"):
            waiting.append(c)
            fresh_request = fresh_request or res == "requested"
            continue
        if res:  # запам'ятати одразу, навіть якщо наступний канал не вийде
            track_joined(state, c, g.get("endAt"), gid)
    if any(r is None for r in results):
        state["failed"][gid] = state["failed"].get(gid, 0) + 1
        return
    if waiting:
        # пробуємо взяти участь одразу після заявки — раптом MRKT зарахує
        if not fresh_request:
            return  # заявка вже пробувалась раніше — чекаємо схвалення
        log.info("  заявка в %s — пробую взяти участь одразу", ", ".join("@" + c for c in waiting))
        if not await validate(api, gid, chans, 25):
            log.info("  MRKT поки не зарахував заявку — візьму участь, коли приймуть")
            return

    if not await validate(api, gid, chans, cfg["validation_timeout_sec"]):
        log.warning("  MRKT не підтвердив підписку — спробую пізніше")
        state["failed"][gid] = state["failed"].get(gid, 0) + 1
        return

    await api.participate(gid)
    state["joined"][gid] = {"endAt": g.get("endAt"), "channels": chans, "at": int(time.time())}
    CTL.stats["joined"] += 1
    log.info("  ✓ Участь прийнята!")
    await CTL.notify(f"✅ Участь у розіграші\nКанали: {', '.join('@' + c for c in chans)}\n"
                     f"Кінець: {fmt_dt(g.get('endAt'))}")


def _left(ts: float) -> str:
    d = int(ts - time.time())
    if d <= 0:
        return "вже"
    days, rem = divmod(d, 86400)
    return (f"{days} д " if days else "") + f"{rem // 3600} год {rem % 3600 // 60} хв"


def _ts(v) -> float:
    if not v:
        return 0.0
    return v.timestamp() if hasattr(v, "timestamp") else float(v)


async def auctions_text(tg) -> str:
    """Активні аукціони подарунків Telegram — один статичний знімок (не оновлюється)."""
    if tg is None:
        return "Юзербот ще не підключений"
    res = await tg(GetStarGiftActiveAuctionsRequest(hash=0))
    auctions = getattr(res, "auctions", None) or []
    if not auctions:
        return "🔨 Зараз нема активних аукціонів подарунків Telegram"
    out = [f"🔨 Активні аукціони Telegram ({len(auctions)}):"]
    for a in auctions:
        g, st, us = a.gift, a.state, a.user_state
        lines = [f"\n🎁 {getattr(g, 'title', None) or f'Подарунок {g.id}'}"]
        if hasattr(st, "gifts_left"):
            total = getattr(g, "availability_total", None)
            lines.append(f"Залишилось: {st.gifts_left}" + (f" з {total}" if total else ""))
            per = getattr(g, "gifts_per_round", None)
            lines.append(f"Раунд: {st.current_round}/{st.total_rounds}" + (f" (по {per} шт.)" if per else ""))
            lines.append(f"Мін. ставка: {st.min_bid_amount} ⭐")
            if st.next_round_at:
                lines.append(f"Наступний раунд: {datetime.fromtimestamp(st.next_round_at):%d.%m %H:%M}"
                             f" (через {_left(st.next_round_at)})")
        end = _ts(getattr(st, "end_date", None))
        if end:
            lines.append(f"⏰ Кінець: {datetime.fromtimestamp(end):%d.%m %H:%M} (через {_left(end)})")
        if getattr(us, "bid_amount", None):
            lines.append(f"Твоя ставка: {us.bid_amount} ⭐")
        if getattr(g, "auction_slug", None):
            lines.append(f"t.me/auction/{g.auction_slug}")
        out.append("\n".join(lines))
    out.append(f"\n(станом на {datetime.now():%d.%m %H:%M})")
    return "\n".join(out)


def gift_name(gift) -> str:
    if not isinstance(gift, dict):
        return "подарунок"
    name = (gift.get("title") or gift.get("name") or gift.get("collectionTitle")
            or gift.get("collectionName") or gift.get("giftName") or "подарунок")
    num = gift.get("number")
    return f"{name} #{num}" if num else str(name)


async def announce_win(api: Mrkt, state: dict, gid: str, gifts: list[str] | None = None) -> None:
    """Повідомлення про виграш у бота-пульта (один раз на розіграш)."""
    announced = state.setdefault("announced", [])
    if not gid or gid in announced:
        return
    announced.append(gid)
    v = state["joined"].get(gid, {})
    v["won"] = True
    CTL.stats["won"] += 1
    if not gifts:
        try:
            g = await api.req("GET", f"/giveaways/{gid}")
            gifts = [gift_name(x) for x in (g.get("gifts") or [g.get("previewGift")]) if x]
        except Exception:
            gifts = []
    save_state(state)
    prize = ", ".join(gifts) if gifts else "подарунок"
    chans = ", ".join("@" + c for c in v.get("channels", [])) or "?"
    log.info("🏆 ВИГРАШ у розіграші %s: %s", gid, prize)
    await CTL.notify(f"🏆 ТИ ВИГРАВ!\n\nПриз: {prize}\nРозіграш від: {chans}\n\n"
                     f"Подарунок у MRKT → Мої подарунки")


async def check_wins(api: Mrkt, state: dict, my_id: int) -> None:
    """1) сповіщення MRKT про виграш (як у самому аппі), 2) запасний варіант — список переможців."""
    try:
        notes = await api.req("GET", "/giveaway-notifications")
        notes = notes if isinstance(notes, list) else (notes.get("items") or [])
        for n in notes:
            if n.get("type") != "Finish":  # у аппі все, що не "Finish", показується як "Win"
                await announce_win(api, state, n.get("giveawayId"))
    except Exception as e:
        log.debug("giveaway-notifications: %s", e)

    now = time.time()
    for gid, v in list(state["joined"].items()):
        if v.get("checked") or not parse_dt(v.get("endAt")) or parse_dt(v.get("endAt")) + 120 > now:
            continue
        try:
            g = await api.req("GET", f"/giveaways/{gid}")
        except MrktError as e:
            log.debug("check win %s: %s", gid, e)
            continue
        winners = g.get("winners")
        if winners is None:
            continue  # результати ще не готові
        v["checked"] = True
        mine = [w for w in winners if str(w.get("userId")) == str(my_id)]
        if mine:
            await announce_win(api, state, gid, [gift_name(w.get("gift")) for w in mine])
        save_state(state)


async def win_watcher(api: Mrkt, state: dict, my_id: int) -> None:
    """Окремо від основного циклу: перевіряє виграші кожну хвилину."""
    while True:
        try:
            await check_wins(api, state, my_id)
        except Exception as e:
            log.debug("win_watcher: %s", e)
        await asyncio.sleep(60)


async def check_pending(tg, cfg: dict, state: dict) -> None:
    """Заявки в канали: коли схвалили — мют + архів + запам'ятати для відписки."""
    pending = state.setdefault("pending", {})
    for key, p in list(pending.items()):
        if parse_dt(p.get("endAt")) and parse_dt(p.get("endAt")) < time.time():
            pending.pop(key)  # розіграш уже закінчився
            continue
        try:
            ent = await tg.get_entity(p["channel"])
        except Exception:
            continue
        if getattr(ent, "left", True) is False:
            pending.pop(key)
            log.info("✅ Заявку в @%s схвалили", p["channel"])
            if cfg["mute_and_archive"]:
                await mute_and_archive(tg, ent, p["channel"])
            track_joined(state, p["channel"], p.get("endAt"), p.get("gid", ""))
            state["failed"].pop(p.get("gid", ""), None)
        await asyncio.sleep(0.5)
    save_state(state)


def setup_bot_blocker(tg: TelegramClient, cfg: dict, state: dict) -> None:
    """Блокує ботів, які пишуть першими після заявки в канал (капчі/реклама від каналів)."""
    never = {"mrkt", "botfather", "pasivch1kbot"} | {b.lower().lstrip("@") for b in cfg["never_block"]}

    @tg.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_dm(ev):
        if not cfg["block_request_bots"]:
            return
        sender = await ev.get_sender()
        if not getattr(sender, "bot", False):
            return
        uname = (sender.username or "").lower()
        if uname in never or (CTL.bot_id and sender.id == CTL.bot_id):
            return
        # тільки якщо нещодавно (48 год) подавали заявку в канал
        if time.time() - state.get("last_request_at", 0) > 48 * 3600:
            return
        # якщо ти сам колись писав цьому боту — це твій бот, не чіпаємо
        async for _ in tg.iter_messages(sender, from_user="me", limit=1):
            return
        try:
            await tg(BlockRequest(id=await tg.get_input_entity(sender)))
            await tg(EditPeerFoldersRequest([InputFolderPeer(await tg.get_input_entity(sender), folder_id=1)]))
            who = f"@{sender.username}" if sender.username else (sender.first_name or f"id {sender.id}")
            log.info("🚫 Заблокував бота %s (написав після заявки)", who)
            await CTL.notify(f"🚫 Заблокував бота {who} — написав після заявки в канал")
        except Exception as e:
            log.warning("не вдалось заблокувати бота id %s: %s", sender.id, e)


async def leave_finished(tg, state: dict) -> None:
    """Відписка від каналів, на які підписав бот, коли розіграш закінчився (+1 год запасу)."""
    keep_needed = set()
    now = time.time()
    for item in state["to_leave"]:
        if parse_dt(item.get("after")) + 3600 > now:
            keep_needed.update(c.lower() for c in item["channels"])
    remaining = []
    for item in state["to_leave"]:
        if parse_dt(item.get("after")) + 3600 > now:
            remaining.append(item)
            continue
        for c in item["channels"]:
            if c.lower() in keep_needed:
                continue  # цей канал ще потрібен іншому активному розіграшу
            try:
                await tg(LeaveChannelRequest(await tg.get_entity(c)))
                log.info("  − відписався від @%s", c)
                await asyncio.sleep(random.uniform(3, 8))
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                log.debug("leave @%s: %s", c, e)
    state["to_leave"] = remaining
    # чистимо старі записи, щоб state не ріс вічно
    state["joined"] = {k: v for k, v in state["joined"].items()
                       if parse_dt(v.get("endAt")) + 7 * 86400 > now}
    save_state(state)


async def one_pass(tg, api: Mrkt, cfg: dict, state: dict, dry: bool, my_id: int = 0) -> None:
    if not dry:
        await check_pending(tg, cfg, state)
    gws = await api.free_giveaways()
    todo = []
    for g in gws:
        reason = why_skip(g, cfg, state)
        if reason:
            log.debug("skip %s: %s", g.get("id"), reason)
        else:
            todo.append(g)
    log.info("Безплатних активних: %d, підходять: %d", len(gws), len(todo))

    if dry:
        for g in gws:
            chans = g.get("chanels") or g.get("channels") or []
            print(f"  {g.get('id')}  канали={chans}  -> {why_skip(g, cfg, state) or 'ВІЗЬМУ УЧАСТЬ'}")
        return

    for g in todo:
        if CTL.paused:
            break
        try:
            await handle_giveaway(tg, api, g, cfg, state)
        except MrktRegionBlocked as e:
            # не рахуємо як невдачу розіграшу — це блок сервера, а не розіграшу
            CTL.blocked_until = time.time() + 3600
            log.error("⛔ MRKT блокує підтвердження участі з цього сервера: %s", e)
            if time.time() - CTL.blocked_notified > 6 * 3600:
                CTL.blocked_notified = time.time()
                await CTL.notify("⛔ MRKT блокує підтвердження участі з IP цього сервера "
                                 "(403 Forbidden Section: Playhub). Підписки йдуть, але участь не "
                                 "зараховується. Потрібен сервер в іншому регіоні.")
            save_state(state)
            break
        except MrktError as e:
            log.warning("  помилка MRKT: %s", e)
            CTL.stats["errors"] += 1
            state["failed"][g["id"]] = state["failed"].get(g["id"], 0) + 1
        save_state(state)
        await rsleep(cfg["giveaway_delay_sec"])

    if cfg["leave_after_end"]:
        await leave_finished(tg, state)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один прохід і вихід")
    ap.add_argument("--dry", action="store_true", help="тільки показати розіграші, нічого не робити")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "bot.log", encoding="utf-8")],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    cfg = load_config()
    state = load_state()
    if os.environ.get("PORT"):
        await health_server(int(os.environ["PORT"]))
    session = StringSession(cfg["string_session"]) if cfg["string_session"] \
        else str(BASE / cfg["session_name"])
    tg = TelegramClient(session, cfg["api_id"], cfg["api_hash"])
    if cfg["string_session"]:
        await tg.connect()
        if not await tg.is_user_authorized():
            sys.exit("TG_SESSION недійсна — згенеруй нову: python export_session.py")

    def ask_phone() -> str:
        while True:
            p = input("Номер телефону твого Telegram (напр. +380XXXXXXXXX), НЕ токен бота: ").strip()
            if ":" in p or not p.lstrip("+").replace(" ", "").isdigit():
                print("Це не номер телефону. Потрібен саме номер акаунта, з якого береш участь.")
                continue
            return p

    await tg.start(phone=ask_phone)  # перший раз попросить номер телефону і код з Telegram
    me = await tg.get_me()
    if me.bot:
        await tg.log_out()
        sys.exit("Сесія залогінена як бот — так не можна. Запусти ще раз і введи НОМЕР ТЕЛЕФОНУ.")
    log.info("Telegram: %s (id %s)", me.first_name, me.id)

    backup = StateBackup(tg) if cfg["state_in_telegram"] else None
    if backup:
        try:
            restored = await backup.restore()
        except Exception as e:
            restored = None
            log.warning("Не вдалось відновити стан з Telegram: %s", e)
        if restored and len(json.dumps(restored)) >= len(json.dumps(state)):
            state.clear()
            state.update(restored)
            save_state(state)
            log.info("Стан відновлено з «Збраного» Telegram")

    api = Mrkt(tg, cfg["proxy"])
    CTL.state = state
    CTL.tg = tg
    if cfg["mute_and_archive"] and not args.dry:
        await archive_tracked(tg, state)
    if not args.dry:
        try:
            await CTL.setup(cfg, me.id)
        except Exception as e:
            log.error("Бот-пульт не запустився (перевір bot_token): %s", e)
            CTL.bot = None
        setup_bot_blocker(tg, cfg, state)
        asyncio.get_running_loop().create_task(win_watcher(api, state, me.id))
    last_err = ""
    try:
        while True:
            if not CTL.paused:
                try:
                    await one_pass(tg, api, cfg, state, args.dry, me.id)
                    CTL.last_pass = datetime.now().strftime("%H:%M:%S")
                    last_err = ""
                    if backup and not args.dry:
                        await backup.push(state)
                except Exception as e:
                    CTL.stats["errors"] += 1
                    if isinstance(e, MrktError):
                        log.error("MRKT: %s", e)
                    else:
                        log.exception("Неочікувана помилка")
                    if str(e) != last_err:  # не спамити однаковою помилкою
                        last_err = str(e)
                        await CTL.notify(f"⚠️ Помилка: {str(e)[:300]}")
            if args.once or args.dry:
                break
            CTL.wake.clear()
            try:
                await asyncio.wait_for(CTL.wake.wait(), cfg["check_interval_sec"])
            except asyncio.TimeoutError:
                pass
    finally:
        if backup and not args.dry:
            await backup.push(state, force=True)
        await CTL.notify("🛑 MRKT бот зупинено")
        await api.http.close()
        if CTL.bot:
            await CTL.bot.disconnect()
        await tg.disconnect()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # emoji в консолі Windows
        except Exception:
            pass
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
