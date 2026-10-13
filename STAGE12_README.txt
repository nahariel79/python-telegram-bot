Stage 12 - Clean cards + daily/monthly/all-time ranked lists + CSV export

Changes:
- Removed daily leaders from every new-call card and every profit milestone card.
- Every new-call and milestone post now links to: Daily leaders, Monthly leaders (last 30 days), All-time.
- Weekly list/button is retired and replaced by Monthly.
- Old weekly stats posts are removed automatically when possible.
- List entries are ranked by ATH profit percent, highest first.
- Each list entry clearly shows token, MC/FDV at recommendation, recommendation date/time in Israel, ATH profit %, multiple, and ATH value.
- Token symbol links back to the original Telegram recommendation when the channel is public.
- Every list page includes a Download CSV button. Followers are taken to the bot and receive the selected list as a UTF-8 CSV file. First-time users may need to press Start in the bot once.
- All-time page keeps the overall statistics: total calls, cumulative ATH PNL, and average ATH profit.
- Lists include the live-data note.
- Existing PostgreSQL call/history data is preserved.

Upload these files:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py
