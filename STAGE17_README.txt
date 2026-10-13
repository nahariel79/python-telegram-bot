Stage 17 - recurring beta notice + private support inbox

New features
============
1. Recurring channel notice (default every 12 hours)
   - Says the channel is in testing / improvement stages.
   - Invites followers to report bugs.
   - Invites followers to ask questions privately.
   - Includes buttons for bug report and private message.
   - Last sent time is stored in PostgreSQL, so restarts/deploys do not create duplicates.

2. Private support through the bot
   - /support starts a private question/contact flow.
   - /bug starts a bug-report flow.
   - The recurring channel notice opens these flows via buttons.
   - Text and screenshots/photos are supported.
   - The message is copied privately to ADMIN_USER_ID.
   - Admin gets a "reply privately" button.
   - The admin's next message is delivered back to that follower through the bot.

Railway variables
=================
Required for support:
ADMIN_USER_ID=<your Telegram numeric user id>

Optional:
BETA_NOTICE_ENABLED=true
BETA_NOTICE_HOURS=12

BETA_NOTICE_HOURS can be changed to 6, 12, 24, etc. Minimum is 1 hour.
The default is 12 hours.

Files
=====
Replace/upload:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py
bot/notice.py   <-- NEW

Existing PostgreSQL data is preserved.
