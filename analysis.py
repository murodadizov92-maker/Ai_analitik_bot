"""Kunlik hisobotdan keyingi tahlil: oylik holat, reyting, muammolar va yechimlar, agentlarga buyruq.
Hammasi qoidalarga asoslangan (tashqi AI kerak emas), qoidalar shu faylda ochiq turibdi."""
import calendar
import datetime as dt
import html
import math
import re
from collections import defaultdict

import report as _rep
from report import fmt_min, money, short, status, VISIT_CAP_MIN

E = lambda x: html.escape(str(x), quote=False)


# ---------------------------------------------------------------- tarixiy ma'lumot
def order_date(o):
    return (o.get("dateDocument") or o.get("dateCreate") or "")[:10]


async def _fetch_range(client, a, b, statuses):
    out, cur = [], a
    while cur <= b:
        end = min(cur + dt.timedelta(days=6), b)
        out += await client.paginate(
            "getOrder",
            {"filter": {"agent": "all", "status": statuses,
                        "period": {"date": {"from": str(cur), "to": str(end)}}}}, "order")
        cur = end + dt.timedelta(days=1)
    return out


def _sum_by(orders):
    per_agent_day, per_day = defaultdict(lambda: defaultdict(float)), defaultdict(float)
    for o in orders:
        d = order_date(o)
        aid = ((o.get("agent") or {}).get("SD_id"))
        v = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        per_agent_day[aid][d] += v
        per_day[d] += v
    return per_agent_day, per_day


def _client_sums(orders, since):
    """{agent_id: {mijoz_nomi: summa}} — filial/ofis yukini mijozlar kesimida ko'rish uchun."""
    out = defaultdict(lambda: defaultdict(float))
    for o in orders:
        if order_date(o) < since:
            continue
        aid = (o.get("agent") or {}).get("SD_id")
        c = (o.get("client") or {}).get("clientName") or (o.get("client") or {}).get("SD_id") or "—"
        out[aid][c] += float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
    return out


async def collect_history(client, day, statuses):
    first = day.replace(day=1)
    start = min(first, day - dt.timedelta(days=7))
    pm_last = first - dt.timedelta(days=1)
    pm_first = pm_last.replace(day=1)
    pm_end = pm_first.replace(day=min(day.day, pm_last.day))
    cur = await _fetch_range(client, start, day, statuses)
    prev = await _fetch_range(client, pm_first, pm_end, statuses)
    return {"cur": _sum_by(cur), "prev": _sum_by(prev), "first": first, "day": day,
            "cur_c": _client_sums(cur, str(first)), "prev_c": _client_sums(prev, "")}


def _metrics(hist):
    day, first = hist["day"], hist["first"]
    cur_a, cur_t = hist["cur"]
    prev_a, prev_t = hist["prev"]
    mtd_t = sum(v for d, v in cur_t.items() if d >= str(first))
    mtd_a = {a: sum(v for d, v in dd.items() if d >= str(first)) for a, dd in cur_a.items()}
    prev_total = sum(prev_t.values())
    prev_a_tot = {a: sum(dd.values()) for a, dd in prev_a.items()}
    base_days = [str(day - dt.timedelta(days=i)) for i in range(1, 8)]
    work_days = [d for d in base_days if cur_t.get(d, 0) > 0]
    base_team = sum(cur_t.get(d, 0) for d in work_days) / len(work_days) if work_days else 0
    base_agent = {a: (sum(dd.get(d, 0) for d in work_days) / len(work_days)) if work_days else 0
                  for a, dd in cur_a.items()}
    return dict(mtd_t=mtd_t, mtd_a=mtd_a, prev_total=prev_total, prev_a=prev_a_tot,
                base_team=base_team, base_agent=base_agent, work_days=len(work_days))


# ---------------------------------------------------------------- oylik prognoz va reja
norm_name = _rep.norm_name


def parse_agent_plans(text):
    """'Aziz=400000000; Bobur=350000000' -> {'aziz': 4e8, 'bobur': 3.5e8}"""
    out = {}
    for part in re.split(r"[;,\n]", text or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[norm_name(k)] = float(re.sub(r"[^\d.]", "", v))
            except ValueError:
                pass
    return out


def month_forecast(S, h, names, day, plan, stretch_pct=5.0, agent_plans=None):
    """Jamoa va har bir agent bo'yicha oylik prognoz. Prognoz = oy boshidan + (ish kuniga o'rtacha x qolgan ish kunlari).
    Ish kunlari: oxirgi ~2 oyda savdo bo'lgan hafta kunlari asosida aniqlanadi (dam olish kunlari hisobga olinmaydi)."""
    m = _metrics(h)
    cur_a, cur_t = h["cur"]
    prev_a, prev_t = h["prev"]
    first = h["first"]
    days_in = calendar.monthrange(day.year, day.month)[1]
    worked = max(len([d for d, v in cur_t.items() if d >= str(first) and v > 0]), 1)
    wd = {dt.date.fromisoformat(d).weekday() for d, v in list(cur_t.items()) + list(prev_t.items()) if v > 0 and d}
    if not wd:
        wd = set(range(6))
    rem = sum(1 for i in range(1, days_in - day.day + 1) if (day + dt.timedelta(days=i)).weekday() in wd)
    sp = stretch_pct / 100
    mtd = m["mtd_t"]
    pace = mtd / worked
    fc = mtd + pace * rem
    fc_recent = mtd + m["base_team"] * rem if m["base_team"] else fc
    need = lambda target: (max(target - mtd, 0) / rem) if rem else 0
    team = dict(plan=plan, mtd=mtd, worked=worked, rem=rem, pace=pace, forecast=fc, forecast_recent=fc_recent,
                pct=fc / plan if plan else 0, need_plan=need(plan), need_stretch=need(plan * (1 + sp)),
                stretch_pct=stretch_pct, days_in=days_in)

    nm = lambda a: names.get(a) or (S[a]["name"] if a in S else str(a))
    ids = [a for a in (set(m["mtd_a"]) | set(S))
           if (m["mtd_a"].get(a, 0) > 0 or a in S) and norm_name(nm(a)) not in _rep.IGNORE]
    ap = {}
    for a in ids:
        k = norm_name(nm(a))
        if agent_plans and k in agent_plans:
            ap[a] = agent_plans[k]
    rest = [a for a in ids if a not in ap]
    if agent_plans:
        # Aniq agent rejalari berilgan bo'lsa, rejasi yo'q agentlarga reja BERILMAYDI (qolgan summa taqsimlanmaydi)
        for a in rest:
            ap[a] = 0
    else:
        # Aksincha, jamoa rejasi o'tgan oy ulushiga qarab bo'linadi
        w = {a: m["prev_a"].get(a, 0) for a in rest}
        if sum(w.values()) == 0:
            w = {a: 1 for a in rest}
        tw = sum(w.values()) or 1
        for a in rest:
            ap[a] = plan * w[a] / tw
    agents = {}
    for a in ids:
        a_mtd = m["mtd_a"].get(a, 0)
        a_pace = a_mtd / worked
        a_fc = a_mtd + a_pace * rem
        a_plan = ap.get(a, 0)
        nd = lambda t: (max(t - a_mtd, 0) / rem) if rem else 0
        agents[a] = dict(name=nm(a), field=(nm(a) or "").strip().lower() not in _rep.NON_FIELD, mtd=a_mtd, pace=a_pace, forecast=a_fc, plan=a_plan,
                         pct=(a_fc / a_plan) if a_plan else None, need=nd(a_plan), need_stretch=nd(a_plan * (1 + sp)))
    team["plans_sum"] = sum(x["plan"] for x in agents.values())
    team["auto_split"] = not agent_plans
    return {"team": team, "agents": agents}


def agent_month_text(s, mon, team_ticket):
    """Agent uchun: reja/ziyod uchun kunlik talab va uni qanday bajarish (zakas soni yoki zakas ulushi)."""
    if not mon or not mon["plan"]:
        return None
    ticket = s["orders_sum"] / s["orders_n"] if s and s["orders_n"] >= 3 else team_ticket
    extra = max(mon["need_stretch"] - mon["pace"], 0)
    if extra <= 0:
        return "temp yetarli: shu sur'atni saqlasa, reja va ziyodi bajariladi"
    txt = []
    if ticket:
        txt.append(f"kuniga yana ~{math.ceil(extra / ticket)} ta zakas (o'rtacha zakas {short(ticket)})")
    if s and s["visited"] and ticket:
        need_conv = (mon["need_stretch"] / ticket) / s["visited"]
        if need_conv <= 1:
            txt.append(f"yoki zakas ulushini {need_conv * 100:.0f}% ga ko'tarish ({s['visited']} vizitda)")
        else:
            txt.append("faqat zakas ulushi yetmaydi: o'rtacha zakasni ham oshirish (zakasga +1-2 tovar) kerak")
    return "; ".join(txt)


# ---------------------------------------------------------------- otkaz sabablari
REASONS = [
    ("assortiment", r"достаточно ассортимент|ассортимент.*достаточ|assortiment (yetarli|kifoya)|yetarli assortiment"),
    ("raqobat", r"эксклюзив|конкурент|raqob|boshqa (dan|yetkaz)|другой постав|^\s*-\s*\S"),
    ("tovar_yoq", r"tovar yo.?q|yo.?q tovar|нет в наличии|нет товара|mavjud emas"),
    ("qarz", r"qarz|долг|debt"),
    ("pul", r"\bpul\b|деньг|нет денег"),
    ("narx", r"narx|qimmat|цен|дорог|price"),
    ("ostatka", r"ostatka|zaxira|остаток|bor edi|\bbor\b|есть|sklad"),
    ("yopiq", r"yopiq|закрыт|closed|egasi yo.?q|kirib bo.?lmad|отсутств"),
]

REASON_PLAYBOOK = {
    "tovar_yoq": ("Tovar yo'q / sklad muammosi",
                  "Mijoz so'ragan tovar skladda bo'lmagani uchun zakas berilmayapti.",
                  ["Eng ko'p so'ralgan, ostatkasi tugayotgan tovarlar ro'yxatini tuzing va zavodga zakasni oldinga suring.",
                   "Agentlarga har kuni ertalab 'bugun skladda mavjud tovarlar' ro'yxatini yuboring, ular faqat mavjudini sotsin.",
                   "Tovar yo'qligi sabab otkaz bo'lsa, agent o'sha tovar nomini yozib qoldirsin, shunda talab hisobi chiqadi."],
                  "Mavjud tovarlar ro'yxatiga tayanib, o'rniga boshqa shunga o'xshash tovarni taklif qiling."),
    "qarz": ("Mijozda qarz bor",
             "Qarzi bor mijozlar yangi zakas bermayapti yoki agent bermaslikka harakat qilyapti.",
             ["Qarzdor mijozlar ro'yxatini agentlarga bering: har birida qarz summasi va muddati bo'lsin.",
              "Qarz yig'ishni zakas bilan birga qo'ying: avval qisman to'lov, keyin yangi zakas.",
              "Qarz limitini belgilang va limitga yetganda zakasni to'xtatish qoidasini jamoaga e'lon qiling."],
             "Qarzdor mijozga kirganingizda avval to'lov kelishuvini oling, keyin qisqa zakas taklif qiling."),
    "pul": ("Mijozda pul yo'q",
            "Mijoz pul yo'qligini aytib zakasni rad etyapti.",
            ["Kichik hajmli, tez aylanadigan tovarlardan boshlab 'minimal zakas' taklif qiling.",
             "Pul tushadigan kunlarni (bozor kunlari, oylik kunlari) aniqlab, marshrutni shunga moslang.",
             "Ishonchli mijozlar uchun to'lov muddatini qisqa kelishuv bilan bering."],
            "Pul yo'q desa, eng kichik hajmdagi tez sotiladigan 1-2 tovarni taklif qiling."),
    "narx": ("Narx qimmat deyilyapti",
             "Mijozlar narxni raqobatchidan qimmat deb bahonalayapti.",
             ["Agentlarga 'qimmat' e'tirozi uchun tayyor javob bering: foydasi, aylanish tezligi, mijozning marjasi.",
              "Raqobatchi narxlarini 10-15 do'kondan yig'ib, haqiqiy farqni aniqlang. Farq kichik bo'lsa, muammo narxda emas.",
              "Qimmat tovarni emas, mijoz uchun foydali (tez sotiladigan) tovarni birinchi taklif qilishni o'rgating."],
             "Narxdan qochmang: tovarning mijozga beradigan foydasini va tez aylanishini ayting."),
    "ostatka": ("Mijozda ostatka bor",
                "Mijozda zaxira bor, shuning uchun zakas bermayapti.",
                ["Agentlar do'konda ostatkani sanab, nechta kunga yetishini hisoblasin va zakasni shu asosda taklif qilsin.",
                 "Ostatkasi bor mijozga boshqa (ostatkasi yo'q) tovarlarni taklif qilishni o'rgating.",
                 "Mijozga keyingi vizit sanasini aniq ayting va o'sha kuni zakas olinadigan qilib kelishing."],
                "Ostatka bor bo'lsa, boshqa tovarni taklif qiling va keyingi zakas sanasini kelishib oling."),
    "yopiq": ("Do'kon yopiq / egasi yo'q",
              "Agent borganda do'kon yopiq yoki qaror qiluvchi odam yo'q.",
              ["Mijozlarning ish soatini aniqlab, marshrutni shu vaqtga moslang.",
               "Yopiq do'konga kun oxirida yana bir marta kirish qoidasini kiriting.",
               "Doimiy yopiq turadigan mijozlarni marshrutdan chiqarib, o'rniga yangi mijoz qo'shing."],
              "Yopiq do'konga kun oxirida qayta kiring va telefon orqali egasiga bog'laning."),
    "raqobat": ("Raqobatchi / eksklyuziv shartnoma",
                "Mijoz raqobatchidan oladi yoki raqobatchi bilan eksklyuziv shartnomasi bor.",
                ["Eksklyuziv shartnomali do'konlar ro'yxatini ajrating: ularga vizit chastotasini kamaytirib, vaqtni boshqa mijozlarga yo'naltiring.",
                 "Qaysi raqobatchi, qaysi tovarlar bo'yicha ekanini yig'ing.",
                 "Mijozga raqobatchida yo'q yoki bizda ustun bo'lgan tovarlarni taklif qilishni topshiring.",
                 "Yetkazib berish tezligi va ishonchliligini asosiy afzallik qilib ko'rsating."],
                "Raqobatchida yo'q tovarlarni va yetkazib berish tezligimizni asosiy argument qiling."),
    "assortiment": ("Mijoz 'assortiment yetarli' deydi",
                    "Mijoz bizning mavjud tovarlarimizni yetarli deb hisoblaydi: agent yangi yoki kam sotiladigan tovarni ko'rsatmayapti yoki taklif mijozga foydali ko'rinmayapti.",
                    ["Har vizitda mijoz polkasida bizda bor, lekin do'konda yo'q tovarlarni ko'rsating: 'Sizda X yo'q, qo'shsak ...' deb aniq pozitsiya taklif qiling.",
                     "Har hafta jamoa uchun 2-3 ta 'fokus tovar' belgilang (yangi yoki kam sotiladigan) va hamma agent shuni taklif qilsin.",
                     "Qo'shni do'konlarda eng yaxshi sotilayotgan tovarlarni misol qilib ko'rsating ('shu tovar yaqin do'konlarda tez ketyapti').",
                     "Ikkilanayotgan mijozga kichik hajmli sinov zakas taklif qiling, keyingi vizitda natijani birga ko'ring."],
                    "'Assortiment yetarli' desa, polkada yo'q bitta tovarni aniq ko'rsatib, kichik sinov zakas taklif qiling."),
    "boshqa": ("Boshqa sabablar",
               "Otkaz sababi aniq kategoriyaga tushmadi.",
               ["Agentlardan otkaz sababini aniq va to'liq yozishni talab qiling (faqat 'yo'q' emas).",
                "Hafta oxirida eng ko'p takrorlangan sabablarni birga ko'rib chiqing."],
               "Otkaz bo'lsa sababini aniq yozing, shunda biz chora ko'ramiz."),
}


def classify(reason):
    r = (reason or "").lower()
    for key, pat in REASONS:
        if re.search(pat, r):
            return key
    return "boshqa"


def classify_tags(reason):
    """Sales Doctor'da otkaz sababi bir necha tanlov bo'lishi mumkin ('Есть долг,Нет денег,...'):
    vergul bo'yicha ajratib, har birini alohida kategoriyaga qo'yadi. Qaytaradi: (kategoriyalar to'plami, tanlovlar soni)."""
    tags = [t.strip() for t in (reason or "").split(",") if t.strip()]
    return {classify(t) for t in tags}, len(tags)


# ---------------------------------------------------------------- agent ko'rsatkichlari
def conv_of(s, use_visit_flag):
    if not s["visited"]:
        return None
    n = s["with_order"] if use_visit_flag else len(s["order_clients"] - {None})
    return min(n, s["visited"]) / s["visited"]


def pct(x):
    return f"{x * 100:.0f}%"


def arrow(cur, base):
    if not base:
        return ""
    d = (cur - base) / base
    return f"{'▲' if d >= 0 else '▼'}{abs(d) * 100:.0f}%"


def agent_metrics(S, h):
    """Jamoa o'rtachalari faqat MAYDON agentlari bo'yicha (ofis savdosi hisobga olinmaydi)."""
    F = [s for s in S.values() if s["field"]]
    use_flag = any(s["with_order"] for s in F)
    tot_vis = sum(s["visited"] for s in F)
    tot_conv_n = sum((s["with_order"] if use_flag else len(s["order_clients"] - {None})) for s in F)
    team_conv = tot_conv_n / tot_vis if tot_vis else 0
    tot_orders = sum(s["orders_n"] for s in F)
    team_ticket = sum(s["orders_sum"] for s in F) / tot_orders if tot_orders else 0
    team_lines = sum(s["lines"] for s in F) / tot_orders if tot_orders else 0
    return use_flag, team_conv, team_ticket, team_lines


def issues_for(s, ctx, h, target):
    """Agent muammolari: (muhimlik, sarlavha, sabab, [qadamlar]). Eng muhimi birinchi."""
    use_flag, team_conv, team_ticket, team_lines = ctx
    out = []
    if not s["field"]:
        return out
    conv = conv_of(s, use_flag)
    vis_rate = s["visited"] / s["planned"] if s["planned"] else None
    first = s["first"]
    total_min = (s["last"] - s["first"]).total_seconds() / 60 if s["first"] else 0

    # vizitlar haqiqiyligi: soniyalik vizitlar, GPS yo'q, yopilmagan vizitlar
    sus = (s["short"] >= 3) or (s["visited"] >= 5 and len(s["no_gps"]) / s["visited"] > 0.5)
    if sus:
        bits = []
        if s["short"]:
            bits.append(f"{s['short']} ta vizit 1 daqiqadan qisqa")
        if s["visited"] and s["no_gps"]:
            bits.append(f"{len(s['no_gps'])} / {s['visited']} vizit GPS siz")
        if s["open_long"]:
            bits.append(f"{s['open_long']} ta vizit {VISIT_CAP_MIN}+ daqiqa yopilmagan")
        out.append((95, "Vizitlar haqiqiyligi shubhali: " + ", ".join(bits),
                    "Do'konga bormasdan, ketma-ket soniyalarda vizit belgilangan bo'lishi mumkin. Bunday vizitlar hisobotdagi vizit sonini oshiradi, lekin savdo bermaydi.",
                    ["Agent bilan suhbatlashing, nega GPS yoqilmaganini va vizitlar nega soniyalarda belgilanganini so'rang (telefon, internet yoki odat bo'lishi mumkin).",
                     "Qoida: vizit faqat do'kon oldida GPS bilan ochiladi va yopiladi. GPS siz yoki 1 daqiqadan qisqa vizit hisobga olinmaydi.",
                     "Supervayzer shu agentning bir necha do'koniga tasodifiy qo'ng'iroq qilib, bugun agent kelganini tasdiqlasin.",
                     "Vizitni vaqtida yopish: kun oxirigacha ochiq qolgan vizitlar uchun ogohlantirish bering."]))
    if conv is not None and s["visited"] >= 5 and conv < target and conv >= target * 0.85:
        out.append((60, f"Zakas ulushi maqsadga yaqin, lekin hali past: {pct(conv)} (maqsad {pct(target)})",
                    "Natija yomon emas, lekin yana bir-ikki do'kondan zakas olinsa maqsadga yetiladi.",
                    ["Zakas bermagan do'konlarga kun oxirida qayta kirish yoki telefon qilish.",
                     "Eng ko'p otkaz bergan do'konlarda sababni aniq yozib, keyingi vizitga tayyor taklif tayyorlash."]))
    elif conv is not None and s["visited"] >= 5 and conv < target:
        gap = target - conv
        if vis_rate is None or vis_rate >= 0.85:
            out.append((90 + gap * 10,
                        f"Vizit bor, lekin zakas kam: {s['visited']} vizitdan {int(round(conv * s['visited']))} tasida zakas ({pct(conv)}, jamoa {pct(team_conv)})",
                        "Agent do'konga kirmoqda, lekin vizit savdoga aylanmayapti: tashrif 'ko'rinish uchun' bo'lib qolgan.",
                        ["Qoida: zakas olmasdan, yoki aniq otkaz sababisiz do'kondan chiqmaslik.",
                         "Har vizitda ostatkani sanash va kamida 3 xil tovar taklif qilish (kamida bitta yangi/kam sotiladigan).",
                         "Zakas bermagan do'konlarga kun oxirida qayta kirish yoki telefon qilish.",
                         "Supervayzer 1-2 vizitda agent bilan birga yurib, savdo suhbatini kuzatsin."]))
        else:
            out.append((70 + gap * 10,
                        f"Zakas ulushi past ({pct(conv)}) va reja bajarilishi {pct(vis_rate)}",
                        "Ham vizit kam, ham ularning savdoga aylanishi past.",
                        ["Avval vizit rejasini to'ldirish: marshrutni zonalarga bo'lib, ertalab 08:30 gacha boshlash.",
                         "Keyin har vizitda 'zakas olamiz' qoidasini kiritish."]))
    if vis_rate is not None and vis_rate < 0.7 and s["planned"] >= 5:
        causes = []
        if first and first.hour * 60 + first.minute > 10 * 60 + 15:
            causes.append(f"ishni kech boshlagan ({first:%H:%M})")
        if s["max_gap"] > 90:
            causes.append(f"{fmt_min(s['max_gap'])} tanaffus")
        if total_min and total_min < 300:
            causes.append(f"ish vaqti qisqa ({fmt_min(total_min)})")
        out.append((80 - vis_rate * 10,
                    f"Reja bajarilmadi: {s['visited']} / {s['planned']} ({pct(vis_rate)})",
                    (", ".join(causes) if causes else "vizitlar orasidagi vaqt yo'qolishi yoki marshrut noqulayligi").capitalize() + ".",
                    ["Ish boshlash vaqtini aniq belgilang (masalan 08:30) va birinchi vizitni nazorat qiling.",
                     "Marshrutni geografik yaqinlik bo'yicha tuzing, 90 daqiqadan uzoq tanaffusga ruxsat bermang.",
                     "Reja har kuni haddan tashqari katta bo'lsa, uni haqiqatga moslab qayta tuzing."]))
    if s["orders_n"] >= 3 and team_ticket and s["orders_sum"] / s["orders_n"] < 0.7 * team_ticket:
        avg = s["orders_sum"] / s["orders_n"]
        out.append((60, f"O'rtacha zakas past: {money(avg)} so'm (jamoa {money(team_ticket)})",
                    "Zakaslar mayda: agent mijozga ozgina tovar taklif qilmoqda.",
                    ["Har zakasga kamida 1-2 qo'shimcha tovar taklif qilish (cross-sell).",
                     "Mijoz odatda oladigan hajmni 1 qadam oshirib taklif qilish (to'plam, katta qadoq).",
                     "Eng yaxshi agentning zakas tarkibini namuna qilib ko'rsating."]))
    if s["orders_n"] >= 3 and team_lines and s["lines"] / s["orders_n"] < 0.7 * team_lines:
        out.append((55, f"Assortiment tor: zakasda o'rtacha {s['lines'] / s['orders_n']:.1f} xil tovar (jamoa {team_lines:.1f})",
                    "Agent bir xil 1-2 tovar sotyapti, mijozda boshqa tovarlar sotilmay qolmoqda.",
                    ["Har mijozga 'bugun yangi tovar' sifatida kamida 1 ta yangi pozitsiya taklif qilish.",
                     "Mijozning polkasini ko'rib, bizning qaysi tovarimiz yo'qligini aniqlash."]))
    b = h["base_agent"].get(s["id"], 0)
    if b > 0 and s["orders_sum"] < 0.7 * b:
        out.append((75, f"Savdo tushib ketdi: {money(s['orders_sum'])} (oxirgi ish kunlari o'rtachasi {money(b)}, {arrow(s['orders_sum'], b)})",
                    "Odatdagidan ancha past savdo.",
                    ["Agent bilan suhbatlashib sababni aniqlang: kasallik, tovar yo'qligi, marshrut yoki mijozlar yo'qolishi.",
                     "Odatda zakas beradigan, lekin kecha bermagan mijozlarga qayta kirishni topshiring."]))
    if s["orders_sum"] and s["ret_sum"] / s["orders_sum"] > 0.03:
        out.append((65, f"Vozvrat ko'p: savdoning {pct(s['ret_sum'] / s['orders_sum'])} ({money(s['ret_sum'])} so'm)",
                    "Mijoz tovarni qaytarmoqda: muddati, sifati yoki ortiqcha zakas sababli bo'lishi mumkin.",
                    ["Vozvrat sabablarini tovar bo'yicha ko'ring: muddat bo'lsa, kichik partiya va tez-tez yetkazishga o'ting.",
                     "Mijozga ortiqcha zakas qildirmaslik: ostatka va sotuv tezligiga qarab zakas olish."]))
    if s["visited"] >= 8 and s["durs"]:
        avg_v = sum(s["durs"]) / len(s["durs"])
        if avg_v < 4:
            out.append((50, f"Vizitlar juda qisqa: o'rtacha {fmt_min(avg_v)}",
                        "Vizit 'sirpanib o'tish' bo'lishi mumkin: savdo suhbatiga vaqt yo'q.",
                        ["Minimal vizit vaqtini (masalan 7-10 daqiqa) belgilab, GPS va foto bilan tekshiring."]))
    if s["visited"] >= 5 and (len(s["no_photo"]) / s["visited"] > 0.3 or len(s["no_gps"]) / s["visited"] > 0.3):
        out.append((45, f"Nazorat buzilgan: fotosiz {len(s['no_photo'])}, GPS siz {len(s['no_gps'])} vizit",
                    "Vizitlar tekshirib bo'lmaydi.",
                    ["Foto otchyot va GPS ni majburiy qoida qiling; bajarmagan vizit hisoblanmasin."]))
    out.sort(key=lambda x: -x[0])
    return out


# ---------------------------------------------------------------- agentlarga buyruq
def agent_command_body(s, ctx, h, target, mon=None):
    """Agentga beriladigan buyruq matni (ismsiz)."""
    use_flag = ctx[0]
    conv = conv_of(s, use_flag)
    if not s["visited"]:
        return "kecha vizit qayd etilmagan. Bugun rejadagi barcha mijozlarga kirib, GPS va foto bilan vizitni qayd eting."
    parts = []
    base = max(s["planned"], s["visited"])
    if conv is not None and conv < target:
        done = int(round(conv * s["visited"]))
        goal = max(math.ceil(target * base), done + 1)
        parts.append(f"kecha {s['visited']} vizitdan {done} tasida zakas ({pct(conv)}). Bugun maqsad: kamida {goal} ta zakas")
    else:
        parts.append(f"zo'r ish, zakas ulushi {pct(conv or 0)}. Shu tempni saqlang va o'rtacha zakasni oshiring")
    if s["no_order_clients"]:
        names = ", ".join(E(c) for c in s["no_order_clients"][:4])
        parts.append(f"zakas bermagan do'konlarga qayta kiring: {names}")
    if s["planned"] and s["visited"] / s["planned"] < 0.8:
        parts.append(f"vizit rejasini to'liq bajaring ({s['visited']}/{s['planned']})")
    if s["no_photo"] or s["no_gps"]:
        parts.append("har vizitda foto va GPS majburiy")
    if mon and mon["plan"]:
        parts.append(f"oylik rejangiz {short(mon['plan'])}, bugun kamida {short(mon['need_stretch'])} so'm savdo qiling")
    return "; ".join(parts) + "."


def agent_command(s, ctx, h, target, mon=None):
    return f"• <b>{E(s['name'])}</b>: " + agent_command_body(s, ctx, h, target, mon)


def command_items(S, ctx, h, target, month=None):
    """Har bir maydon agenti uchun alohida buyruq: [{'id','name','body'}]"""
    ag = (month or {}).get("agents", {})
    items = []
    for s in sorted(S.values(), key=lambda x: x["name"]):
        if not s["field"]:
            continue
        body = agent_command_body(s, ctx, h, target, ag.get(s["id"]))
        items.append({"id": s["id"], "name": s["name"], "body": body[:1].upper() + body[1:]})
    return items


def build_commands(S, day, h, ctx, target, month=None):
    head = (f"📣 <b>Agentlarga bugungi buyruq</b>\n"
            f"1) Har bir do'kondan zakas olamiz. Zakassiz yoki aniq sababsiz chiqmaymiz.\n"
            f"2) Do'konga kirganda ostatkani sanang, kamida 3 xil tovar taklif qiling.\n"
            f"3) Otkaz bo'lsa sababini aniq yozing, foto va GPS ni unutmang.\n"
            f"Maqsad: zakas ulushi kamida {target * 100:.0f}% (kecha jamoada {pct(ctx[1])}).")
    ag = (month or {}).get("agents", {})
    lines = [agent_command(s, ctx, h, target, ag.get(s["id"]))
             for s in sorted(S.values(), key=lambda x: x["name"]) if s["field"]]
    return [head + "\n\n" + "\n".join(lines)]


# ---------------------------------------------------------------- rahbar uchun tahlil
def build_manager(S, idle, day, h, ctx, target, month=None):
    m = _metrics(h)
    use_flag, team_conv, team_ticket, team_lines = ctx
    first = day.replace(day=1)
    days_in = calendar.monthrange(day.year, day.month)[1]
    elapsed = day.day
    L = [f"🧠 <b>Tahlil va yechimlar — {day:%d.%m.%Y}</b>"]

    # --- oylik reja va prognoz
    day_total = sum(s["orders_sum"] for s in S.values())
    t = month["team"] if month else None
    if t:
        L.append("\n📅 <b>Oylik reja, prognoz va yechim</b>")
        L.append(f"Reja: <b>{money(t['plan'])}</b> · oy boshidan: {money(t['mtd'])} ({t['mtd'] / t['plan'] * 100:.0f}%) · {t['worked']} ish kuni o'tdi, {t['rem']} qoldi")
        L.append(f"Prognoz: <b>{money(t['forecast'])}</b> ({t['pct'] * 100:.0f}%) {status(t['pct'])} · oxirgi hafta sur'ati bilan {money(t['forecast_recent'])}")
        if m["prev_total"]:
            L.append(f"O'tgan oyning shu davriga nisbatan: {arrow(t['mtd'], m['prev_total'])}")
        if t.get("auto_split"):
            L.append("⚠️ Bu oy uchun agent rejalari kiritilmagan: jamoa rejasi o'tgan oy ulushiga qarab taxminan bo'lingan.")
        if t["plan"] and not t.get("auto_split") and abs(t["plans_sum"] - t["plan"]) / t["plan"] > 0.01:
            L.append(f"⚠️ Agent rejalari yig'indisi {money(t['plans_sum'])}, jamoa rejasi {money(t['plan'])}: farq {money(t['plans_sum'] - t['plan'])}. Qaysi biri to'g'ri ekanini aniqlang.")
        gap_plan = max(t["plan"] - t["mtd"], 0)
        L.append(f"Rejaga yetish uchun: kuniga <b>{money(t['need_plan'])}</b> (hozir {money(t['pace'])}) · ziyod ({t['stretch_pct']:.0f}%+): kuniga <b>{money(t['need_stretch'])}</b>")
        ag_all = month["agents"].values()
        field_pace = sum(x["pace"] for x in ag_all if x["field"])
        other_pace = sum(x["pace"] for x in ag_all if not x["field"])
        field_mtd = sum(x["mtd"] for x in ag_all if x["field"])
        other_mtd = sum(x["mtd"] for x in ag_all if not x["field"])
        if t["mtd"] and other_mtd:
            L.append(f"🏢 Ofis/to'g'ridan-to'g'ri savdo oy savdosining {other_mtd / t['mtd'] * 100:.0f}% ini beradi ({short(other_mtd)}); maydon agentlari: {short(field_mtd)} (kuniga ~{short(field_pace)})")
        # ofis sur'ati o'zgarmaydi deb, qolgan ziyodni maydon agentlari qoplaydi
        need_field = max(t["need_stretch"] - other_pace, 0)
        extra = max(need_field - field_pace, 0)
        n_ag = max(len([s for s in S.values() if s["field"] and s["visited"]]), 1)
        if t["rem"] and extra > 0 and team_ticket:
            L.append(f"\n🎯 <b>Ziyod bajarish uchun maydon agentlari kuniga yana {money(extra)} so'm sotishi kerak</b> (ofis hozirgi sur'atda deb hisoblanganda). Yo'llari (biri yoki aralash):")
            L.append(f"1) Zakas sonini oshirish: kuniga jami ~{math.ceil(extra / team_ticket)} ta zakas ko'proq (har agentga ~{math.ceil(extra / team_ticket / n_ag)} ta, o'rtacha zakas {money(team_ticket)} so'm)")
            if field_pace:
                L.append(f"2) O'rtacha zakasni +{extra / field_pace * 100:.0f}% oshirish: zakasga 1-2 qo'shimcha tovar")
            tot_vis = sum(s["visited"] for s in S.values() if s["field"])
            if tot_vis and team_conv and field_pace:
                need_c = team_conv * ((field_pace + extra) / field_pace)
                L.append(f"3) Zakas ulushini {pct(team_conv)} dan {pct(min(need_c, 1.0))} ga ko'tarish" + (" (faqat shu yetmaydi, zakasni ham kattalashtirish kerak)" if need_c > 1 else ""))
            L.append("\nEng tez natija beradigan ishlar (ketma-ketlikda):")
            lv = []
            if any((x["short"] >= 3 or (x["visited"] >= 5 and len(x["no_gps"]) / x["visited"] > 0.5)) for x in S.values() if x["field"]):
                lv.append("Vizit haqiqiyligini tartibga solish: GPS majburiy, soniyalik vizitlar hisoblanmasin. Aks holda vizit soni haqiqiy ishni ko'rsatmaydi.")
            if team_conv < target:
                lv.append(f"Zakas ulushini {target * 100:.0f}% ga yetkazish: har vizitda zakas qoidasi, zakas bermaganlarga kun oxirida qayta kirish.")
            if team_lines and team_lines < 3:
                lv.append(f"Assortiment: zakasda {team_lines:.1f} xil tovar. Har zakasga kamida 1 ta qo'shimcha tovar taklif qilish.")
            lv.append("Har kuni ertalab 'zakas bermagan mijozlar' ro'yxatini agentlarga berib, kech soatlarda ularga qayta tashrif/qo'ng'iroq qildirish.")
            if idle:
                lv.append("Ishlamayotgan agentlar bilan ishlash: ularning marshruti boshqa agentlarga vaqtincha berilsin.")
            lv.append("Eng kuchli agent usulini jamoaga ko'rsatish va sust agentlarni uning bilan 1-2 kunga juftlash.")
            for i, x in enumerate(lv, 1):
                L.append(f"   {i}. {E(x)}")
        elif t["rem"] and extra <= 0:
            L.append("✅ Hozirgi sur'at bilan reja va ziyod ham bajariladi. Sur'atni tushirmaslik uchun har kuni zakas ulushini kuzating.")
        L.append("\n📌 <b>Agentlar bo'yicha reja va prognoz</b>")
        for aid, a in sorted(month["agents"].items(), key=lambda kv: -kv[1]["forecast"]):
            sA = S.get(aid)
            if not a["plan"]:
                L.append(f"⚪ {'🏢 ' if not a['field'] else ''}<b>{E(a['name'])}</b>: oy {short(a['mtd'])} · prognoz {short(a['forecast'])} · reja belgilanmagan")
                continue
            L.append(f"{status(a['pct'])} {'🏢 ' if not a['field'] else ''}<b>{E(a['name'])}</b>: oy {short(a['mtd'])} · prognoz {short(a['forecast'])} / reja {short(a['plan'])} ({a['pct'] * 100:.0f}%)")
            tx = agent_month_text(sA, a, team_ticket) if a["field"] else None
            if tx:
                L.append(f"   Ziyod uchun kuniga {short(a['need_stretch'])} kerak (hozir {short(a['pace'])}): {E(tx)}")
    else:
        L.append("\n(Oylik reja kiritilmagan)")
    # --- ofis / filiallar kesimi
    if month:
        for aid, ag in month["agents"].items():
            if ag["field"]:
                continue
            cc = (h.get("cur_c") or {}).get(aid) or {}
            pc = (h.get("prev_c") or {}).get(aid) or {}
            if not cc:
                continue
            tot_c = sum(cc.values()) or 1
            L.append(f"\n🏢 <b>{E(ag['name'])}: mijozlar (filial/ofis yuki) bo'yicha, oy boshidan</b>")
            for cname, v in sorted(cc.items(), key=lambda kv: -kv[1])[:6]:
                p = pc.get(cname, 0)
                L.append(f"• {E(cname)}: {short(v)} ({v / tot_c * 100:.0f}%)"
                         + (f" · o'tgan oyning shu davrida {short(p)} ({arrow(v, p)})" if p else " · o'tgan oyning shu davrida yuk bo'lmagan"))
            if ag["plan"] and ag["pct"] is not None and ag["pct"] < 1:
                L.append(f"Prognoz rejadan {short(ag['plan'] - ag['forecast'])} kam. Bu savdo agent harakatiga emas, filial va mijozlar buyurtmasiga bog'liq. Yechim:")
                for n, st_ in enumerate([
                        "Yuki pasaygan filialdan sababini so'rang: skladda ostatka yetarlimi yoki ularning o'z savdosi sustmi.",
                        "Har filial o'z agentlarining oylik rejasiga mos yuk rejasini bersin (filial sotuv rejasi + zaxira = ofis yuk rejasi).",
                        "Yuk ritmini belgilang (masalan haftada 2-3 aniq kun) va zavodga zakas berish jadvali bilan moslang, shunda tovar uzilmaydi.",
                        "Filial skladlarida sust sotiladigan tovarlarni kamaytirib, tez sotiladiganlarni ko'proq yuklang."], 1):
                    L.append(f"   {n}) {E(st_)}")

    # --- reyting
    L.append("\n🏆 <b>Agentlar reytingi</b> (savdo · zakas ulushi · oy boshidan)")
    ranked = sorted(S.values(), key=lambda x: -x["orders_sum"])
    for i, s in enumerate(ranked, 1):
        cv = conv_of(s, use_flag)
        mt = m["mtd_a"].get(s["id"], 0)
        pm = m["prev_a"].get(s["id"], 0)
        L.append(f"{i}. {'🏢 ' if not s['field'] else ''}{E(s['name'])} — {money(s['orders_sum'])} · {pct(cv) if (cv is not None and s['field']) else '—'} · oy: {money(mt)}"
                 + (f" ({arrow(mt, pm)})" if pm else ""))
    best = max((s for s in S.values() if s["field"] and conv_of(s, use_flag) is not None), key=lambda s: conv_of(s, use_flag), default=None)
    if best and conv_of(best, use_flag) > team_conv + 0.1:
        L.append(f"💡 Eng yuqori zakas ulushi: {E(best['name'])} ({pct(conv_of(best, use_flag))}). Uning har bir do'kondagi ishlash usulini boshqalarga ko'rsating.")

    # --- jamoa ko'rsatkichlari
    L.append("\n📊 <b>Jamoa ko'rsatkichlari</b>")
    L.append(f"Zakas ulushi: {pct(team_conv)} (maqsad {target * 100:.0f}%) · o'rtacha zakas: {money(team_ticket)} so'm · zakasda {team_lines:.1f} xil tovar")
    pays = sum(s["pay_sum"] for s in S.values())
    if day_total:
        L.append(f"Tushum / savdo: {pct(pays / day_total)} ({money(pays)} / {money(day_total)})")

    # --- agent muammolari
    L.append("\n⚠️ <b>Muammolar va yechimlar</b>")
    any_issue = False
    for s in ranked:
        iss = issues_for(s, ctx, h | {"base_agent": m["base_agent"]}, target)[:2]
        if not iss:
            continue
        any_issue = True
        L.append(f"\n👤 <b>{E(s['name'])}</b>")
        for _, title, cause, steps in iss:
            L.append(f"🔸 {E(title)}")
            L.append(f"   Sabab: {E(cause)}")
            L.append("   Nima qilish:")
            for n, st in enumerate(steps, 1):
                L.append(f"   {n}) {E(st)}")
    if not any_issue:
        L.append("Jiddiy muammo topilmadi, yaxshi ish.")

    # --- otkaz sabablari
    cnt = defaultdict(int)
    n_rej, n_tags = 0, 0
    for s in S.values():
        for _, r in s["rejects"]:
            cats, k_tags = classify_tags(r)
            n_rej += 1
            n_tags += k_tags
            for c in cats:
                cnt[c] += 1
    if cnt:
        L.append(f"\n🚫 <b>Otkaz sabablari (jamoa, {n_rej} ta otkazli vizit)</b>")
        for k, n in sorted(cnt.items(), key=lambda x: -x[1]):
            L.append(f"• {E(REASON_PLAYBOOK[k][0])}: {n} ta vizitda ({n * 100 // n_rej}%)")
        if n_rej and n_tags / n_rej >= 2.5:
            L.append(f"⚠️ Bir otkazda o'rtacha {n_tags / n_rej:.1f} ta sabab belgilangan: sabablar formal belgilanayotgan bo'lishi mumkin. Agentdan faqat bitta ASOSIY sababni tanlashni talab qiling, aks holda tahlil aniq bo'lmaydi.")
        top = max(cnt, key=cnt.get)
        name, cause, steps, cmd = REASON_PLAYBOOK[top]
        L.append(f"Eng ko'p sabab: <b>{E(name)}</b>. {E(cause)}")
        for n, st in enumerate(steps, 1):
            L.append(f"   {n}) {E(st)}")
        L.append(f"📣 Agentlarga: «{E(cmd)}»")

    # --- umumiy ogohlantirishlar
    notes = []
    if day_total and pays / day_total < 0.5:
        notes.append("Tushum savdoning yarmidan kam: qarz yig'ish uchun agentlarga qarzdor mijozlar ro'yxatini bering va to'lov kelishuvini vizit natijasiga qo'shing.")
    if idle:
        notes.append(f"Faoliyat yo'q agentlar: {', '.join(E(x) for x in idle[:8])}. Sababini aniqlang (ta'til, kasallik yoki ishlamagan).")
    if t and t['pct'] < 0.95:
        notes.append("Prognoz rejadan past: eng tez yo'l zakas ulushini oshirish va zakas bermagan do'konlarga qayta kirish.")
    if notes:
        L.append("\n📌 <b>Rahbar uchun</b>")
        for n in notes:
            L.append(f"• {n}")
    return _chunk("\n".join(L))


def _chunk(text, limit=3900):
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        parts.append(cur)
    return parts
