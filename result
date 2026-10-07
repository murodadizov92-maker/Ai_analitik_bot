"""«Резултат»: oy boshidan shu vaqtgacha har bir agent (va OFIS) bo'yicha reja, savdo (yuk chiqqan) va tushgan pul."""
import html

import analysis
import report
from report import short, status


def mln(x):
    return f"{x / 1e6:.0f}" if abs(x) >= 1e9 else f"{x / 1e6:.1f}"


def row(mark, name, plan, zakas, pct, ship, pay):
    return f"{mark}{name:<10} {plan:>6} {zakas:>6} {pct:>4} {ship:>6} {pay:>6}"


async def collect_pays(client, first, day):
    pays = await client.paginate(
        "getPayment", {"filter": {"transactionType": 3, "period": {"date": {"from": str(first), "to": str(day)}}}}, "payment")
    out = {}
    for p in pays:
        aid = (p.get("agent") or {}).get("SD_id")
        out[aid] = out.get(aid, 0.0) + float(p.get("amount") or 0)
    return out


async def collect_ship(client, first, day, statuses, date_filter):
    """Oy boshidan chiqqan yuk (yuborilgan/yetkazilgan/yopilgan buyurtmalar): {agent_id: summa}"""
    orders = await client.paginate(
        "getOrder", {"filter": {"agent": "all", "status": statuses,
                                "period": {date_filter: {"from": str(first), "to": str(day)}}}}, "order")
    out = {}
    for o in orders:
        aid = (o.get("agent") or {}).get("SD_id")
        out[aid] = out.get(aid, 0.0) + float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
    return out


def build(month, pays, ship, day, title):
    """month: analysis.month_forecast natijasi; pays: {agent_id: tushgan pul}. Qaytaradi: [xabar matnlari]"""
    e = lambda x: html.escape(str(x), quote=False)
    t = month["team"]
    ag = sorted(month["agents"].items(), key=lambda kv: -(kv[1]["plan"] or kv[1]["mtd"]))
    tot_plan = sum(a["plan"] for _, a in ag)
    tot_sales = sum(a["mtd"] for _, a in ag)
    tot_pay = sum(pays.get(aid, 0) for aid, _ in ag)
    tot_ship = sum(ship.get(aid, 0) for aid, _ in ag)

    head = (f"📊 <b>{e(title)}: natija, {day.replace(day=1):%d.%m} dan {day:%d.%m.%Y} gacha</b>\n"
            f"Oyning {t['worked']} ish kuni o'tdi, {t['rem']} qoldi · summalar mln so'mda\n"
            "Zakas = agentlar olgan buyurtmalar · Yuk = ombordan chiqqan (yetkazishga) · Pul = tushgan pul")
    rows = ["  " + f"{'Agent':<10} {'Reja':>6} {'Zakas':>6} {'%':>4} {'Yuk':>6} {'Pul':>6}"]
    for aid, a in ag:
        fc = status(a["pct"]) if a["plan"] else "⚪"
        rows.append(row(fc, a["name"].split("(")[0].strip()[:10], mln(a["plan"]) if a["plan"] else "—", mln(a["mtd"]),
                        f"{a['mtd'] / a['plan'] * 100:.0f}%" if a["plan"] else "—", mln(ship.get(aid, 0)), mln(pays.get(aid, 0))))
    rows.append("—" * 42)
    rows.append(row("  ", "JAMI", mln(tot_plan), mln(tot_sales), f"{tot_sales / tot_plan * 100:.0f}%" if tot_plan else "—",
                    mln(tot_ship), mln(tot_pay)))
    table = "<pre>" + e("\n".join(rows)) + "</pre>"

    green, yellow, red = [], [], []
    for aid, a in ag:
        if not a["plan"]:
            continue
        line = (f"<b>{e(a['name'])}</b>: prognoz {short(a['forecast'])} / reja {short(a['plan'])} ({a['pct'] * 100:.0f}%)")
        if a["pct"] >= 1:
            green.append(line + " · tempni saqlasa reja bajariladi")
        elif a["pct"] >= 0.85:
            yellow.append(line + f" · kuniga {short(a['need'])} kerak (hozir {short(a['pace'])})")
        else:
            red.append(line + f" · kuniga {short(a['need'])} kerak (hozir {short(a['pace'])}), kamomad ~{short(a['plan'] - a['forecast'])}")
    parts = [head, table]
    if t["plan"]:
        parts.append(f"📈 <b>Jamoa prognozi:</b> {short(t['forecast'])} / reja {short(t['plan'])} ({t['pct'] * 100:.0f}%) {status(t['pct'])}")
        parts.append("<b>Reja bo'yicha holat</b>")
    if green:
        parts.append("🟢 Reja bajarilmoqda:\n" + "\n".join("• " + g for g in green))
    if yellow:
        parts.append("🟡 Diqqat, ozgina yetmayapti:\n" + "\n".join("• " + y for y in yellow))
    if red:
        parts.append("🔴 Xavf, tezkor chora kerak:\n" + "\n".join("• " + r for r in red))
    if not (green or yellow or red):
        parts.append("Bu filial uchun oylik agent rejalari kiritilmagan, shuning uchun reja foizi ko'rsatilmadi.")

    # yuk chiqishi: zakasning qancha qismi ombordan chiqqan
    sg, sy, sr = [], [], []
    for aid, a in ag:
        if not a["mtd"]:
            continue
        ratio = ship.get(aid, 0) / a["mtd"]
        line = f"<b>{e(a['name'])}</b>: zakas {mln(a['mtd'])} · yuk {mln(ship.get(aid, 0))} ({ratio * 100:.0f}%)"
        (sg if ratio >= 0.9 else sy if ratio >= 0.75 else sr).append(line)
    sh = []
    if sg:
        sh.append("🟢 Zakaslar o'z vaqtida chiqmoqda:\n" + "\n".join("• " + x for x in sg))
    if sy:
        sh.append("🟡 Yuk biroz orqada:\n" + "\n".join("• " + x for x in sy))
    if sr:
        sh.append("🔴 Zakas olingan, lekin yuk chiqmagan (yetkazilmay qolgan):\n" + "\n".join("• " + x for x in sr))
    if sh:
        parts.append("🚚 <b>Yuk chiqishi</b> (chiqqan yuk / zakas)\n" + "\n\n".join(sh))

    cg, cy, cr = [], [], []
    for aid, a in ag:
        base = ship.get(aid, 0) or a["mtd"]
        if not base:
            continue
        ratio = pays.get(aid, 0) / base
        s_ = f"<b>{e(a['name'])}</b>: pul {mln(pays.get(aid, 0))} / yuk {mln(base)} ({ratio * 100:.0f}%)"
        (cg if ratio >= 0.8 else cy if ratio >= 0.5 else cr).append(s_)
    cash = []
    if cg:
        cash.append("🟢 Pul yaxshi tushmoqda:\n" + "\n".join("• " + x for x in cg))
    if cy:
        cash.append("🟡 O'rtacha:\n" + "\n".join("• " + x for x in cy))
    if cr:
        cash.append("🔴 Yuk chiqdi, pul kam kelgan:\n" + "\n".join("• " + x for x in cr))
    if cash:
        parts.append("💵 <b>Pul kelishi</b> (tushgan pul / chiqqan yuk)\n" + "\n\n".join(cash))

    # asosiy xulosa
    worst = max(((a["plan"] - a["forecast"], a["name"]) for _, a in ag if a["plan"]), default=(0, ""), key=lambda x: x[0])
    if not t["plan"]:
        pass
    elif worst[0] > 0:
        parts.append(f"🎯 <b>Asosiy xulosa:</b> eng katta kamomad — {e(worst[1])} (~{short(worst[0])}). "
                     f"Rejaga yetish uchun jami kuniga {short(t['need_plan'])} kerak, hozir {short(t['pace'])}.")
    else:
        parts.append("🎯 <b>Asosiy xulosa:</b> hozirgi sur'at bilan reja bajariladi. Sur'atni tushirmang.")
    text = "\n\n".join(parts)
    # 4096 chegarasi uchun bo'lish
    out, cur = [], ""
    for blk in text.split("\n\n"):
        if cur and len(cur) + len(blk) + 2 > 3900:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + blk
    if cur:
        out.append(cur)
    return out
