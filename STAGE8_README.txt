Stage 8 - Ranked leaderboards + clearer ATH design

Changes:
- Daily / weekly / all-time channel lists are ranked by ATH profit percentage, highest first.
- Tie-breakers: higher ATH valuation, then newer call.
- Leaderboard text uses HTML bold formatting.
- ATH has its own prominent trophy line.
- Peak profit and X multiple have their own colored emoji badge line.
- Top 3 use medals.
- Daily leaders embedded in each new call use the same ranking and visual hierarchy.
- No database data is deleted; no destructive migration is used.

Files to upload:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py

Actual Telegram text color cannot be set by bots, so colored emoji badges are used for visual distinction.
