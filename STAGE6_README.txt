Stage 6 changes
===============

1. Channel-native lists
   - Recommendation buttons no longer open the bot DM.
   - Daily, weekly, and all-time buttons link directly to permanent posts in the public Telegram channel.
   - The list posts are updated automatically every 60 seconds.
   - Long lists are split into multiple channel pages with Previous/Next links.

2. Daily leaders in every new recommendation
   - Top 3 calls from the last 24 hours.
   - Each line shows ATH profit percentage and ATH valuation.

3. Duplicate-call protection
   - A token address that was published at any time in the past cannot be published again.
   - Checked before preview/publish and enforced by a PostgreSQL trigger as a final safeguard.

4. Existing data remains intact
   - Uses a safe migration and does not drop calls, milestones, users, or historical ATH data.

Files to upload/replace
-----------------------
main.py              -> replace in repository root
bot/db.py             -> replace
bot/handlers.py       -> replace
bot/tracker.py        -> replace
bot/stats.py          -> NEW FILE

Railway variables
-----------------
No new variable is required while CHANNEL_ID is a public username such as:
CHANNEL_ID=@aicryptoisrael

Optional:
STATS_REFRESH_SECONDS=60
CHANNEL_USERNAME=aicryptoisrael

If CHANNEL_ID stops being a public @username, set CHANNEL_USERNAME to the public channel username so direct Telegram post links can be built.
