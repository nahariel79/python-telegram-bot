Stage 9 - Reliable buttons + daily leaders

Fixes:
1. Daily leaders are now loaded/finalized independently of the stats-page refresh.
   A stale/deleted Telegram list post can no longer prevent leaders from appearing.
2. Stale/deleted daily/weekly/all-time list posts are automatically recreated and
   their new Telegram message IDs are saved back to PostgreSQL.
3. Navigation links are rebuilt after any list page is recreated.
4. The new call is always finalized after saving to DB. If list maintenance fails,
   it still receives the daily-leaders block; DexScreener is kept as fallback.
5. No existing PostgreSQL data is dropped or recreated.

Replace all files to avoid mixed versions:
- main.py
- bot/db.py
- bot/handlers.py
- bot/tracker.py
- bot/stats.py
