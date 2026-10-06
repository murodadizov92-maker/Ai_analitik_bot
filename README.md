# Agentlar kunlik hisoboti boti (Sales Doctor -> Telegram)
Har kuni 09:00 (Toshkent) da KECHAGI kun bo'yicha:
1. Hisobot: vizit, ish vaqti, foto/GPS, savdo, tushgan pul, vozvrat, otkaz + oylik reja/prognoz (jami va har agent) + Excel
2. Agentlarga buyruq (har agentga shaxsiy maqsad va oylik talab)
3. Rahbar uchun tahlil: oylik reja va prognoz, ziyod uchun yechim, reyting, muammolar, otkaz sabablari

Buyruqlar: /hisobot, /hisobot bugun, /hisobot 2026-10-04, /id

Render (majburiy): BOT_TOKEN, GROUP_CHAT_ID, SALESDOC_DOMAIN, SALESDOC_LOGIN, SALESDOC_PASSWORD
Ixtiyoriy:
- TOPIC_ID (topikli guruh)
- MONTHLY_PLAN (standart 3200000000), STRETCH_PERCENT (rejadan necha % ziyod, standart 5)
- AGENT_PLANS ("Aziz=400000000; Bobur=350000000"), ko'rsatilmasa reja o'tgan oy ulushi bo'yicha bo'linadi
- ANALYSIS_CHAT_ID (rahbar tahlili alohida chatga), TARGET_CONVERSION (standart 70), REPORT_TIME (09:00), ORDER_STATUSES (1,2,3,4)
