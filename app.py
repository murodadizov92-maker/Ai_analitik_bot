"""Agentlar kunlik hisoboti boti (Render web service).
Har kuni REPORT_TIME da (Toshkent) KECHAGI kun bo'yicha hisobotni guruhga yuboradi."""
import asyncio
import datetime as dt
import html
import logging
import os
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

import analysis
import report
from salesdoc import SalesDocClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def env(name, default=""):
    return os.getenv(name, default).strip()


STATUSES = [int(x) for x in env("ORDER_STATUSES", "1,2,3,4").split(",") if x]
TZ = ZoneInfo(env("TIMEZONE", "Asia/Tashkent"))
REPORT_TIME = env("REPORT_TIME", "09:00")
TARGET_CONV = float(env("TARGET_CONVERSION", "70")) / 100  # zakas ulushi maqsadi (%)
MONTHLY_PLAN = float(env("MONTHLY_PLAN", "3200000000") or 0)  # oylik savdo rejasi (so'm): 3 mlrd 200 mln
STRETCH_PCT = float(env("STRETCH_PERCENT", "5"))              # rejadan necha % ziyod qilish maqsadi
AGENT_PLANS = analysis.parse_agent_plans(env("AGENT_PLANS"))   # ixtiyoriy: "Aziz=400000000; Bobur=350000000"
ANALYSIS_CHAT_ID = int(env("ANALYSIS_CHAT_ID")) if env("ANALYSIS_CHAT_ID") else None  # rahbar tahlili shu yerga
CATCHUP_MIN = int(env("CATCHUP_MINUTES", "90"))
state = {"last_sent": None}
busy = asyncio.Lock()
RT = {}
dp = Dispatcher()


def today():
    return dt.datetime.now(TZ).date()


async def build(client, day):
    async with busy:
        data = await report.collect(client, day, STATUSES)
        try:
            hist = await analysis.collect_history(client, day, STATUSES)
        except Exception:
            log.exception("Tarixiy ma'lumot olinmadi, tahlil qisqartiriladi")
            hist = None
    S, idle = report.summarize(data)
    xlsx = report.make_excel(S, data, day)
    month, cmds, mgr = None, [], []
    if hist is not None:
        names = {a["SD_id"]: a.get("name") for a in data["agents"]}
        ctx = analysis.agent_metrics(S, hist)
        hist = {**hist, "base_agent": analysis._metrics(hist)["base_agent"]}
        if MONTHLY_PLAN:
            month = analysis.month_forecast(S, hist, names, day, MONTHLY_PLAN, STRETCH_PCT, AGENT_PLANS)
        cmds = analysis.build_commands(S, day, hist, ctx, TARGET_CONV, month)
        mgr = analysis.build_manager(S, idle, day, hist, ctx, TARGET_CONV, month)
    msgs = report.build_messages(S, idle, day, month)
    return msgs, xlsx, cmds, mgr


async def _send(bot, chat_id, thread_id, texts):
    for t in texts:
        await bot.send_message(chat_id, t, message_thread_id=thread_id, parse_mode=ParseMode.HTML)


async def deliver(bot, client, chat_id, thread_id, day, manual=False):
    msgs, xlsx, cmds, mgr = await build(client, day)
    await _send(bot, chat_id, thread_id, msgs)
    await bot.send_document(chat_id, BufferedInputFile(xlsx, filename=f"agentlar_{day}.xlsx"),
                            message_thread_id=thread_id)
    await _send(bot, chat_id, thread_id, cmds)
    if ANALYSIS_CHAT_ID and not manual:
        await _send(bot, ANALYSIS_CHAT_ID, None, mgr)
    else:
        await _send(bot, chat_id, thread_id, mgr)


# ------------------------------------------------------------ buyruqlar
@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Chat ID: <code>{m.chat.id}</code>\nTopik ID: <code>{m.message_thread_id}</code>",
                   parse_mode=ParseMode.HTML)


@dp.message(Command("hisobot"))
async def cmd_hisobot(m: Message, command: CommandObject):
    arg = (command.args or "kecha").strip().lower()
    try:
        if arg == "kecha":
            day = today() - dt.timedelta(days=1)
        elif arg == "bugun":
            day = today()
        else:
            day = dt.date.fromisoformat(arg)
    except ValueError:
        await m.answer("Sana noto'g'ri. Misollar: /hisobot, /hisobot bugun, /hisobot 2026-10-04")
        return
    await m.answer(f"⏳ {day:%d.%m.%Y} uchun hisoblayapman...")
    try:
        await deliver(RT["bot"], RT["sd"], m.chat.id, m.message_thread_id, day, manual=True)
    except Exception as e:
        log.exception("hisobot")
        await m.answer(f"❌ Xato: {html.escape(str(e))}", parse_mode=ParseMode.HTML)


# ------------------------------------------------------------ jadval
def _hm(s):
    h, m = map(int, s.split(":"))
    return h, m


async def run_daily(bot, client, chat_id, thread_id):
    if chat_id is None:
        log.warning("GROUP_CHAT_ID berilmagan")
        return
    day = today() - dt.timedelta(days=1)
    if state["last_sent"] == day:
        return
    try:
        await deliver(bot, client, chat_id, thread_id, day)
        state["last_sent"] = day
    except Exception:
        log.exception("Kunlik hisobot yuborilmadi")
        try:
            await bot.send_message(chat_id, "❌ Kunlik agentlar hisobotida xato chiqdi.", message_thread_id=thread_id)
        except Exception:
            pass


async def scheduler(bot, client):
    chat_id = int(env("GROUP_CHAT_ID")) if env("GROUP_CHAT_ID") else None
    thread_id = int(env("TOPIC_ID")) if env("TOPIC_ID") else None
    h, m = _hm(REPORT_TIME)
    await asyncio.sleep(10)
    now = dt.datetime.now(TZ)
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if start <= now <= start + dt.timedelta(minutes=CATCHUP_MIN):
        await run_daily(bot, client, chat_id, thread_id)
    while True:
        now = dt.datetime.now(TZ)
        nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await run_daily(bot, client, chat_id, thread_id)


async def health(_):
    return web.Response(text=f"ok, oxirgi yuborilgan kun: {state['last_sent']}")


async def main():
    bot = Bot(env("BOT_TOKEN"), default_properties=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sd = SalesDocClient(env("SALESDOC_DOMAIN"), env("SALESDOC_LOGIN"), env("SALESDOC_PASSWORD"),
                        env("SALESDOC_FILIAL_ID"))
    RT.update(bot=bot, sd=sd)
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(env("PORT", "10000"))).start()
    task = asyncio.create_task(scheduler(bot, sd))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await sd.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
