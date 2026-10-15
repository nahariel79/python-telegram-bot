Stage 20 - volume-spike momentum alerts

Correction from Stage 19
------------------------
Automatic momentum alerts are now triggered by a sudden increase in 5-minute trading VOLUME, not by upward price movement. Price can be flat or even down when the alert is triggered.

How it works
------------
- DexScreener m5 volume is sampled on every tracker cycle.
- The current rolling 5-minute volume is compared with snapshots from roughly 1 minute and 5 minutes earlier.
- Default trigger thresholds:
  VOLUME_SPIKE_1M_PERCENT=50
  VOLUME_SPIKE_5M_PERCENT=100
  VOLUME_SPIKE_MIN_5M_USD=1000
  MOMENTUM_COOLDOWN_MINUTES=30

Example: if rolling 5m volume was $4,000 one minute ago and is $7,000 now, that is +75% and can trigger the 1m rule.

The alert shows:
- current 5m volume
- volume acceleration versus ~1m and ~5m ago
- original call MC/FDV and time
- current MC/FDV
- current P/L
- ATH
- DexScreener/original-call/dashboard buttons

Database migration
------------------
Stage 20 safely adds volume fields to the existing snapshot and alert tables. No calls or historical data are deleted.
