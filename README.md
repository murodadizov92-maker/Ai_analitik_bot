# Agentlar boti (Sales Doctor -> Telegram)

## Har kuni 09:00 (Toshkent) — KECHAGI kun
- Guruhga: hisobot + Excel (oylik reja/prognoz bilan)
- Admin shaxsiy chatiga: hisobot + Excel + rahbar tahlili + menyu

## Shaxsiy menyu (faqat ADMIN_IDS), /start
- Дустлик / Галлаорол / Жиззах: Bugun, Kecha, Shu hafta, O'tgan hafta, Shu oy, O'tgan oy (hafta dushanba-yakshanba, Toshkent vaqti)
- 📣 Буйруқлар: har agentga alohida buyruq; agent tugmasi -> guruhga agentni belgilab yuboradi; «Hammasiga»
- 📊 Резултат: oy boshidan reja / zakas / yuk chiqqan / pul jadvali + rangli tahlil -> filial guruhiga

## Render Environment
Majburiy: BOT_TOKEN, ADMIN_IDS (vergul bilan Telegram ID), GROUP_CHAT_ID, SALESDOC_DOMAIN, SALESDOC_LOGIN, SALESDOC_PASSWORD
Filiallar: DUSTLIK_LOGIN, DUSTLIK_PASSWORD, DUSTLIK_GROUP_CHAT_ID (+ ixtiyoriy DUSTLIK_DOMAIN, DUSTLIK_TOPIC_ID,
  DUSTLIK_AGENT_PLANS, DUSTLIK_AGENT_TG, DUSTLIK_OFFICE_NAMES, DUSTLIK_IGNORE_AGENTS); xuddi shunday GALLAOROL_*; Жиззах uchun JIZZAX_* (yoki eski nomlar)
Agent Telegram ID: JIZZAX_AGENT_TG = "Ism=123456; Ism2=@username"
Reja: AGENT_PLANS ("Ism=summa; ..."), MONTHLY_PLAN (bo'sh bo'lsa rejalar yig'indisi), STRETCH_PERCENT (5)
Yuk chiqqan: SHIP_DATE_FIELD (standart dateLoad; muqobil: date, dateCreate, dateUpdate), SHIP_STATUSES (standart 2,3,4 = yuborilgan, yetkazilgan, yopilgan)
Boshqa: TARGET_CONVERSION (70), REPORT_TIME (09:00), DAYS_OFF (6 = yakshanba), DAILY_GROUP_COMMANDS (1 bo'lsa har kuni guruhga umumiy buyruq), IGNORE_AGENTS, OFFICE_NAMES (OFIS), ORDER_STATUSES (1,2,3,4)

Komandalar: /id (chat, topik va sizning ID), /start, /hisobot [bugun|kecha|YYYY-MM-DD]
