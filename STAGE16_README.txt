Stage 16 - ATH scale/outlier correction

Fixes a historical OHLCV issue that could turn a real ~$1M ATH into ~$1B.

Changes:
- Separates live-observed ATH from history-derived ATH.
- On first Stage 16 startup, resets public ATH rankings to a clean live baseline
  without deleting any calls, then forces historical reconciliation again.
- Historical OHLCV is validated against the entry price to reject wrong-token/
  wrong-scale series.
- Extreme malformed single-candle wicks are ignored instead of defining ATH.
- A newly validated history value is allowed to correct a previously bad higher
  history value; live-observed highs are never reduced.
- Existing PostgreSQL calls/history remain intact. No DROP TABLE is used.

Optional Railway variables (defaults are already safe):
HISTORY_MAX_ANCHOR_FACTOR=500
HISTORY_MAX_WICK_FACTOR=100
