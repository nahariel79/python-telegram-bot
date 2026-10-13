Stage 14 - Live leaderboards + design + stale-link repair

What changed:
- Daily/monthly/all-time list posts refresh every 30 seconds by default.
- Live tracking of many calls now runs with bounded concurrency (default 6), so DB values are refreshed much faster.
- Historical OHLCV reconciliation handles up to 2 due calls per minute by default and prioritizes newly published calls first.
- Daily/monthly/all-time ranking is strictly highest ATH profit % first.
- Leaderboard design is cleaner: profit/X first, ATH strongly highlighted, entry MC/FDV and call date/time clearly separated.
- Every list displays a LIVE timestamp so subscribers can see when the list was rendered.
- Existing recommendation and milestone posts have their buttons repaired once on startup so they point to the current leaderboard posts, even if a leaderboard Telegram message had previously been recreated.
- CSV download remains available from each leaderboard.
- Existing PostgreSQL data is preserved. No DROP TABLE and no destructive migration.

Optional Railway variables (defaults already work):
STATS_REFRESH_SECONDS=30
TRACK_CONCURRENCY=6
HISTORY_LOOP_SECONDS=60
HISTORY_BATCH_SIZE=2

Upload/replace:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py
