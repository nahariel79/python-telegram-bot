Stage 13 - Milestone name + persistent ranking buttons

Fixes:
- Profit milestone posts now show the full token name plus symbol.
- Daily / monthly / all-time buttons are no longer coupled to stats-page refresh success.
- Missing primary stats pages are created before buttons are built.
- A failure in one ranking period no longer blocks the other periods.
- Removed redundant double refresh of every ranking period.
- No database tables are dropped and existing call history is preserved.

Upload all five files to avoid version mixing:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py
