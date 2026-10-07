"""Agentlar boti (Render web service): kunlik hisobot + shaxsiy menyu.
Menyu (faqat ADMIN_IDS): filiallar -> davr (bugun/kecha/hafta/oy), Буйруқлар (agentga buyruq), Резултат (guruhga)."""
import asyncio
import datetime as dt
import html
import logging
import os
import re
import time
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import analysis
import report
import result
from salesdoc import SalesDocClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
HTML = ParseMode.HTML


def env(name, default=""):
    return os.getenv(name, default).strip()


# ------------------------------------------------------------ umumiy sozlamalar
STATUSES = [int(x) for x in env("ORDER_STATUSES", "1,2,3,4").split(",") if x]
TZ = ZoneInfo(env("TIMEZONE", "Asia/Tashkent"))
REPORT_TIME = env("REPORT_TIME", "09:00")
CATCHUP_MIN = int(env("CATCHUP_MINUTES", "90"))
TARGET_CONV = float(env("TARGET_CONVERSION", "70")) / 100
STRETCH_PCT = float(env("STRETCH_PERCENT", "5"))
ADMIN_IDS = {int(x) for x in re.split(r"[,;\s]+", env("ADMIN_IDS")) if x.isdigit()}
DAYS_OFF = {int(x) for x in env("DAYS_OFF", "6").split(",") if x.strip().isdigit()}  # 6 = yakshanba
DAILY_GROUP_COMMANDS = env("DAILY_GROUP_COMMANDS", "0") == "1"
report.SHIP_FILTER = env("SHIP_DATE_FIELD", "dateLoad")   # "yuk chiqqan" sanasi qaysi maydon bo'yicha olinadi
report.SHIP_STATUSES = [int(x) for x in env("SHIP_STATUSES", "2,3,4").split(",") if x.strip()]

BTN_CMD = "📣 Буйруқлар"
BTN_RES = "📊 Резултат"

# Sales Doctor'dagi oylik agent rejalari: {filial: {"YYYY-MM": "Ism=summa; ..."}}. Render'dagi <FILIAL>_AGENT_PLANS shularni bekor qiladi.
DEFAULT_PLANS = {
    "JIZZAX": {
        "2026-10": "Хусанов Самандар ( Шахар )=150000000; Жабборов Самариддин (Пахтакор)=506000000; "
                   "Усмонов Ғолибжон (Бозор шахар)=526000000; OFIS=2400000000",
    },
}


def parse_tg(text):
    """'Ism=123456; Ism2=@username' -> {normalizatsiyalangan ism: id yoki @username}"""
    out = {}
    for part in re.split(r"[;\n]", text or ""):
        if "=" in part:
            k, v = part.rsplit("=", 1)
            out[report.norm_name(k)] = v.strip()
    return out


class Branch:
    """Bitta filial = bitta Sales Doctor hisobi (login/parol) + o'z guruhi, rejalari va agentlari."""

    def __init__(self, key, title):
        self.key = key
        main = key == "JIZZAX"
        P = key + "_"
        g = lambda name, fallback="": env(P + name) or (env(fallback) if main and fallback else "")
        self.title = env(P + "TITLE") or title
        self.domain = g("DOMAIN", "SALESDOC_DOMAIN") or env("SALESDOC_DOMAIN")
        self.login, self.password = g("LOGIN", "SALESDOC_LOGIN"), g("PASSWORD", "SALESDOC_PASSWORD")
        self.filial = g("FILIAL_ID", "SALESDOC_FILIAL_ID")
        cid, tid = g("GROUP_CHAT_ID", "GROUP_CHAT_ID"), g("TOPIC_ID", "TOPIC_ID")
        self.chat_id = int(cid) if cid.lstrip("-").isdigit() else None
        self.topic_id = int(tid) if tid.isdigit() else None
        self.plans_text = g("AGENT_PLANS", "AGENT_PLANS")
        self.monthly_env = float(g("MONTHLY_PLAN", "MONTHLY_PLAN") or 0)
        self.agent_tg = parse_tg(env(P + "AGENT_TG"))
        names = env(P + "OFFICE_NAMES", "OFIS")
        self.non_field = {report.norm_name(x) for x in names.split(";") if x.strip()}
        ign = env(P + "IGNORE_AGENTS", "Сидикова Хуршида ( Шахар )" if main else "")
        self.ignore = {report.norm_name(x) for x in ign.split(";") if x.strip()}
        self.client = SalesDocClient(self.domain, self.login, self.password, self.filial) if (self.login and self.password) else None
        self.lock = asyncio.Lock()

    @property
    def ready(self):
        return self.client is not None

    def plans_for(self, day):
        txt = self.plans_text or DEFAULT_PLANS.get(self.key, {}).get(f"{day:%Y-%m}", "")
        return analysis.parse_agent_plans(txt)


BRANCHES = {k: Branch(k, t) for k, t in (("DUSTLIK", "Дустлик"), ("GALLAOROL", "Галлаорол"), ("JIZZAX", "Жиззах"))}
BY_TITLE = {b.title: b for b in BRANCHES.values()}
MAIN = BRANCHES["JIZZAX"]

state = {"last_sent": None}
RT = {}
CACHE = {}
dp = Dispatcher()


def today():
    return dt.datetime.now(TZ).date()


def is_admin(uid):
    return uid in ADMIN_IDS


async def cached(key, ttl, factory):
    v = CACHE.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    val = await factory()
    CACHE[key] = (time.time(), val)
    if len(CACHE) > 60:
        for k in sorted(CACHE, key=lambda k: CACHE[k][0])[:20]:
            CACHE.pop(k, None)
    return val


# ------------------------------------------------------------ davrlar (Toshkent vaqti, hafta dushanba-yakshanba)
PERIODS = [("today", "Bugun"), ("yest", "Kecha"), ("week", "Shu hafta"), ("pweek", "O'tgan hafta"),
           ("month", "Shu oy"), ("pmonth", "O'tgan oy")]


def period_range(code, t=None):
    t = t or today()
    if code == "today":
        return t, t, None
    if code == "yest":
        y = t - dt.timedelta(days=1)
        return y, y, None
    if code in ("week", "pweek"):
        mon = t - dt.timedelta(days=t.weekday()) - (dt.timedelta(days=7) if code == "pweek" else dt.timedelta())
        sun = mon + dt.timedelta(days=6)
        end = min(sun, t)
        return mon, end, f"{mon:%d.%m}–{sun:%d.%m.%Y} (dushanba–yakshanba)"
    if code == "month":
        first = t.replace(day=1)
        return first, t, f"{first:%d.%m}–{t:%d.%m.%Y} (shu oy)"
    if code == "pmonth":
        last = t.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=1), last, f"{last.replace(day=1):%d.%m}–{last:%d.%m.%Y} (o'tgan oy)"
    raise ValueError(code)


# ------------------------------------------------------------ hisobot yig'ish
async def load(b, d_from, d_to):
    async with b.lock:
        data = await cached(("data", b.key, d_from, d_to), 300, lambda: report.collect(b.client, d_from, d_to, STATUSES))
        try:
            hist = await cached(("hist", b.key, d_to), 600, lambda: analysis.collect_history(b.client, d_to, STATUSES))
        except Exception:
            log.exception("Tarixiy ma'lumot olinmadi")
            hist = None
    return data, hist


def analyze_sync(b, data, hist, d_to):
    # DIQQAT: bu funksiyada await yo'q: modul darajasidagi filial sozlamalari shu hisob tugaguncha o'zgarmaydi
    report.NON_FIELD, report.IGNORE = b.non_field, b.ignore
    S, idle = report.summarize(data)
    month = ctx = None
    if hist is not None:
        names = {x["SD_id"]: x.get("name") for x in data["agents"]}
        ctx = analysis.agent_metrics(S, hist)
        hist = {**hist, "base_agent": analysis._metrics(hist)["base_agent"]}
        plans = b.plans_for(d_to)
        plan = b.monthly_env or sum(plans.values())
        if plan:
            month = analysis.month_forecast(S, hist, names, d_to, plan, STRETCH_PCT, plans)
    return S, idle, hist, ctx, month


async def make_report(b, d_from, d_to, label=None):
    data, hist = await load(b, d_from, d_to)
    S, idle, hist2, ctx, month = analyze_sync(b, data, hist, d_to)
    multi = d_from != d_to
    msgs = report.build_messages(S, idle, d_to, month, label=label, title=f"{b.title}: agentlar hisoboti", multi=multi)
    return dict(msgs=msgs, xlsx=report.make_excel(S, data, d_to), S=S, idle=idle, hist=hist2, ctx=ctx,
                month=month, multi=multi, day=d_to)


def manager_text(res):
    if res["hist"] is None:
        return ["Tahlil uchun tarixiy ma'lumot olinmadi, keyinroq qayta urinib ko'ring."]
    return analysis.build_manager(res["S"], res["idle"], res["day"], res["hist"], res["ctx"], TARGET_CONV, res["month"])


# ------------------------------------------------------------ yuborish yordamchilari
async def send_texts(chat_id, texts, thread_id=None, markup=None):
    bot = RT["bot"]
    for i, t in enumerate(texts):
        last = i == len(texts) - 1
        await bot.send_message(chat_id, t, message_thread_id=thread_id, parse_mode=HTML,
                               reply_markup=markup if last else None)


async def send_excel(chat_id, xlsx, day, thread_id=None):
    await RT["bot"].send_document(chat_id, BufferedInputFile(xlsx, filename=f"agentlar_{day}.xlsx"),
                                  message_thread_id=thread_id)


def main_menu():
    rows = [[KeyboardButton(text=b.title) for b in BRANCHES.values()],
            [KeyboardButton(text=BTN_CMD), KeyboardButton(text=BTN_RES)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def period_kb(key):
    btns = [InlineKeyboardButton(text=name, callback_data=f"r:{key}:{code}") for code, name in PERIODS]
    return InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)])


def branch_kb(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.title, callback_data=f"{prefix}:{b.key}")]
                                                 for b in BRANCHES.values()])


def mention(b, name):
    v = b.agent_tg.get(report.norm_name(name))
    if v and v.lstrip("-").isdigit():
        return f'<a href="tg://user?id={v}">{html.escape(name)}</a>'
    if v and v.startswith("@"):
        return html.escape(v)
    return f"<b>{html.escape(name)}</b>"


async def deny(m_or_cb):
    uid = m_or_cb.from_user.id
    txt = f"Ruxsat yo'q. Sizning ID: {uid}. Bot egasi bu raqamni ADMIN_IDS ga qo'shishi kerak."
    if isinstance(m_or_cb, CallbackQuery):
        await m_or_cb.answer(txt, show_alert=True)
    else:
        await m_or_cb.answer(txt)


# ------------------------------------------------------------ buyruqlar (komandalar)
@dp.message(Command("id"))
async def cmd_id(m: Message):
    u = m.from_user
    await m.answer(f"Chat ID: <code>{m.chat.id}</code>\nTopik ID: <code>{m.message_thread_id}</code>\n"
                   f"Sizning ID: <code>{u.id}</code>" + (f" · @{u.username}" if u.username else ""), parse_mode=HTML)


@dp.message(Command("start", "menu"))
async def cmd_start(m: Message):
    if m.chat.type != "private":
        return
    if not is_admin(m.from_user.id):
        return await deny(m)
    await m.answer("Menyu tayyor. Filialni tanlang yoki «Буйруқлар» / «Резултат» ni bosing.", reply_markup=main_menu())


@dp.message(Command("hisobot"))
async def cmd_hisobot(m: Message, command: CommandObject):
    if ADMIN_IDS and not is_admin(m.from_user.id):
        return await deny(m)
    arg = (command.args or "kecha").strip().lower()
    try:
        day = {"kecha": today() - dt.timedelta(days=1), "bugun": today()}.get(arg) or dt.date.fromisoformat(arg)
    except ValueError:
        return await m.answer("Sana noto'g'ri. Misollar: /hisobot, /hisobot bugun, /hisobot 2026-10-04")
    await m.answer(f"⏳ {day:%d.%m.%Y} uchun hisoblayapman...")
    await run_report(MAIN, m.chat.id, day, day, None, m.message_thread_id)


# ------------------------------------------------------------ menyu tugmalari
@dp.message(F.chat.type == "private", F.text.in_(set(BY_TITLE)))
async def on_branch(m: Message):
    if not is_admin(m.from_user.id):
        return await deny(m)
    b = BY_TITLE[m.text]
    if not b.ready:
        return await m.answer(f"{b.title} filiali hali sozlanmagan ({b.key}_LOGIN va {b.key}_PASSWORD kerak).")
    await m.answer(f"<b>{b.title}</b>: davrni tanlang", parse_mode=HTML, reply_markup=period_kb(b.key))


@dp.message(F.chat.type == "private", F.text == BTN_CMD)
async def on_cmd_btn(m: Message):
    if not is_admin(m.from_user.id):
        return await deny(m)
    await m.answer("Qaysi filial agentlariga buyruq?", reply_markup=branch_kb("c"))


@dp.message(F.chat.type == "private", F.text == BTN_RES)
async def on_res_btn(m: Message):
    if not is_admin(m.from_user.id):
        return await deny(m)
    await m.answer("Qaysi filial natijasi guruhga yuborilsin?", reply_markup=branch_kb("n"))


# ------------------------------------------------------------ davr bo'yicha hisobot
async def run_report(b, d_from, d_to, label, chat_id, thread_id=None, code=None):
    try:
        res = await make_report(b, d_from, d_to, label)
    except Exception as e:
        log.exception("hisobot")
        return await RT["bot"].send_message(chat_id, f"❌ Xato: {html.escape(str(e))}", message_thread_id=thread_id, parse_mode=HTML)
    kb = None
    if code and not res["multi"] and res["hist"] is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧠 Tahlil va yechimlar", callback_data=f"a:{b.key}:{code}")]])
    await send_texts(chat_id, res["msgs"], thread_id, kb)
    await send_excel(chat_id, res["xlsx"], d_to, thread_id)


@dp.callback_query(F.data.startswith("r:"))
async def cb_report(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await deny(cb)
    _, key, code = cb.data.split(":")
    b = BRANCHES[key]
    await cb.answer("⏳ Hisoblayapman...")
    d_from, d_to, label = period_range(code)
    await run_report(b, d_from, d_to, label, cb.message.chat.id, code=code)


@dp.callback_query(F.data.startswith("a:"))
async def cb_analysis(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await deny(cb)
    _, key, code = cb.data.split(":")
    b = BRANCHES[key]
    await cb.answer("⏳")
    d_from, d_to, label = period_range(code)
    try:
        res = await make_report(b, d_from, d_to, label)
        await send_texts(cb.message.chat.id, manager_text(res))
    except Exception as e:
        log.exception("tahlil")
        await cb.message.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=HTML)


# ------------------------------------------------------------ Буйруқлар: agentga alohida buyruq
async def get_commands(b):
    t = today()
    d = t - dt.timedelta(days=1)
    while d.weekday() in DAYS_OFF:
        d -= dt.timedelta(days=1)

    async def build():
        data, hist = await load(b, d, d)
        S, idle, hist2, ctx, month = analyze_sync(b, data, hist, d)
        return d, (analysis.command_items(S, ctx, hist2, TARGET_CONV, month) if ctx else [])
    return await cached(("cmd", b.key, t), 1800, build)


@dp.callback_query(F.data.startswith("c:"))
async def cb_commands(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await deny(cb)
    b = BRANCHES[cb.data.split(":")[1]]
    if not b.ready:
        return await cb.answer(f"{b.title} sozlanmagan", show_alert=True)
    await cb.answer("⏳ Buyruqlarni tayyorlayapman...")
    try:
        d, items = await get_commands(b)
    except Exception as e:
        log.exception("buyruqlar")
        return await cb.message.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=HTML)
    if not items:
        return await cb.message.answer("Buyruq tayyorlash uchun ma'lumot topilmadi.")
    lines = [f"📣 <b>{b.title}: agentlarga buyruqlar</b> ({d:%d.%m.%Y} natijasi asosida)\n"
             "Agent tugmasini bossangiz, buyruq guruhga shu agentni belgilab yuboriladi.\n"]
    for i, it in enumerate(items, 1):
        warn = "" if report.norm_name(it["name"]) in b.agent_tg else " ⚠️ Telegram ID yo'q"
        lines.append(f"<b>{i}. {html.escape(it['name'])}</b>{warn}\n{it['body']}\n")
    rows = [[InlineKeyboardButton(text=f"📤 {it['name'].split('(')[0].strip()[:28]}", callback_data=f"s:{b.key}:{i}")]
            for i, it in enumerate(items)]
    rows.append([InlineKeyboardButton(text="📤 Hammasiga", callback_data=f"s:{b.key}:all")])
    await send_texts(cb.message.chat.id, analysis._chunk("\n".join(lines)), markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("s:"))
async def cb_send_command(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await deny(cb)
    _, key, which = cb.data.split(":")
    b = BRANCHES[key]
    if not b.chat_id:
        return await cb.answer(f"{b.title} uchun guruh sozlanmagan ({key}_GROUP_CHAT_ID)", show_alert=True)
    try:
        d, items = await get_commands(b)
        if which == "all":
            text = "📣 <b>Bugungi buyruqlar</b>\n\n" + "\n\n".join(f"{mention(b, it['name'])}\n{it['body']}" for it in items)
        else:
            it = items[int(which)]
            text = f"📣 {mention(b, it['name'])}\n{it['body']}"
        await send_texts(b.chat_id, analysis._chunk(text), b.topic_id)
        await cb.answer("✅ Guruhga yuborildi")
    except Exception as e:
        log.exception("buyruq yuborish")
        await cb.answer(f"Xato: {e}"[:190], show_alert=True)


# ------------------------------------------------------------ Резултат: oy boshidan natija -> guruhga
async def make_result(b):
    t = today()
    first = t.replace(day=1)
    async with b.lock:
        hist = await cached(("hist", b.key, t), 600, lambda: analysis.collect_history(b.client, t, STATUSES))
        agents = await b.client.paginate("getAgent", {}, "agent")
        pays = await result.collect_pays(b.client, first, t)
        ship = await result.collect_ship(b.client, first, t, report.SHIP_STATUSES, report.SHIP_FILTER)
    report.NON_FIELD, report.IGNORE = b.non_field, b.ignore   # (await yo'q bo'lgan blok boshlanishi)
    names = {a["SD_id"]: a.get("name") for a in agents}
    stub = {a["SD_id"]: {"name": a.get("name") or a["SD_id"]} for a in agents if a.get("active", "Y") != "N"}
    h = {**hist, "base_agent": analysis._metrics(hist)["base_agent"]}
    plans = b.plans_for(t)
    plan = b.monthly_env or sum(plans.values())
    month = analysis.month_forecast(stub, h, names, t, plan, STRETCH_PCT, plans)
    return result.build(month, pays, ship, t, b.title)


@dp.callback_query(F.data.startswith("n:"))
async def cb_result(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await deny(cb)
    b = BRANCHES[cb.data.split(":")[1]]
    if not b.ready:
        return await cb.answer(f"{b.title} sozlanmagan", show_alert=True)
    await cb.answer("⏳ Natijani hisoblayapman...")
    try:
        texts = await make_result(b)
    except Exception as e:
        log.exception("natija")
        return await cb.message.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=HTML)
    if b.chat_id:
        await send_texts(b.chat_id, texts, b.topic_id)
        await cb.message.answer(f"✅ {b.title} natijasi guruhga yuborildi.")
    else:
        await send_texts(cb.message.chat.id, texts)
        await cb.message.answer(f"⚠️ {b.title} uchun guruh sozlanmagan ({b.key}_GROUP_CHAT_ID), natija shu yerga chiqdi.")


# ------------------------------------------------------------ har kuni 09:00
async def run_daily():
    y = today() - dt.timedelta(days=1)
    if state["last_sent"] == y:
        return
    try:
        res = await make_report(MAIN, y, y)
        if MAIN.chat_id:
            await send_texts(MAIN.chat_id, res["msgs"], MAIN.topic_id)
            await send_excel(MAIN.chat_id, res["xlsx"], y, MAIN.topic_id)
            if DAILY_GROUP_COMMANDS and res["ctx"]:
                await send_texts(MAIN.chat_id, analysis.build_commands(res["S"], y, res["hist"], res["ctx"], TARGET_CONV, res["month"]), MAIN.topic_id)
        for uid in ADMIN_IDS:   # shaxsiy chatga: hisobot + tahlil + menyu
            try:
                await send_texts(uid, res["msgs"])
                await send_excel(uid, res["xlsx"], y)
                await send_texts(uid, manager_text(res) + ["Menyu: filial yoki «Буйруқлар» / «Резултат» ni tanlang 👇"], markup=main_menu())
            except Exception:
                log.exception("Admin chatga yuborilmadi (bot bilan /start qilinganmi?): %s", uid)
        state["last_sent"] = y
    except Exception:
        log.exception("Kunlik hisobot yuborilmadi")
        for cid in ([MAIN.chat_id] if MAIN.chat_id else []) + list(ADMIN_IDS):
            try:
                await RT["bot"].send_message(cid, "❌ Kunlik agentlar hisobotida xato chiqdi.", message_thread_id=MAIN.topic_id if cid == MAIN.chat_id else None)
            except Exception:
                pass


def _hm(s):
    h, m = map(int, s.split(":"))
    return h, m


async def scheduler():
    h, m = _hm(REPORT_TIME)
    await asyncio.sleep(10)
    now = dt.datetime.now(TZ)
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start <= now <= start + dt.timedelta(minutes=CATCHUP_MIN) and MAIN.ready:
        await run_daily()
    while True:
        now = dt.datetime.now(TZ)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        if MAIN.ready:
            await run_daily()


async def health(_):
    return web.Response(text=f"ok, oxirgi yuborilgan kun: {state['last_sent']}")


async def main():
    bot = Bot(env("BOT_TOKEN"))
    RT["bot"] = bot
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(env("PORT", "10000"))).start()
    task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        for b in BRANCHES.values():
            if b.client:
                await b.client.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
