Stage 7 - Visual message redesign

Changes:
- New-call posts use HTML bold/italic/code formatting and clear visual sections.
- Profit milestone posts strongly emphasize the achieved profit target.
- Daily leaders remain inside each new call, with medals and bold percentages.
- Risk warning remains at the bottom of both call and milestone posts.
- Existing database/history/list/duplicate-prevention behavior is unchanged.

Files to upload:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py

Only handlers.py and tracker.py changed from Stage 6, but uploading the full set avoids version mixing.
