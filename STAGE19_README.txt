Stage 19 - repeat-call signals + automatic momentum alerts

New behavior
------------
1. Pasting a CA that was already published no longer offers a second call. Instead the admin gets two manual update buttons:
   - "זמן טוב לכניסה לפני עלייה"
   - "זמן טוב להוסיף צבירה"
   The original entry MC/time remains unchanged.

2. Superseded by Stage 20: automatic momentum detection now uses sudden VOLUME acceleration, not upward price movement.

See STAGE20_README.txt for the current thresholds and logic.

The alert includes token name/symbol, original call valuation and time, current valuation, current profit/loss, ATH, DexScreener, original-call link when available, and dashboard buttons.

Database safety
---------------
Stage 19 only adds new tables through a safe migration:
- price_snapshots
- momentum_alerts
- call_signals
No existing calls are deleted or reset.
