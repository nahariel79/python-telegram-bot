Stage 18 - Public Live Dashboard + safe /deletecall

NEW
- bot/webapp.py: responsive public dashboard served by the same Railway service.
- Buttons on new and existing call/milestone posts open:
  /leaders/day
  /leaders/month
  /leaders/all
- CSV download is available directly from each dashboard page.
- Dashboard snapshot refreshes from PostgreSQL every 15 seconds by default.
- /deletecall <CA | exact symbol | Call ID> lets ADMIN_USER_ID safely remove a bad call.
  Milestones are removed by database cascade, stats update automatically, and the bot tries to delete related Telegram posts.
- Native Telegram leaderboard posts are no longer created or refreshed.
- Existing call/milestone keyboards are periodically repaired to point at the new dashboard.

RAILWAY - REQUIRED ONE-TIME STEP
1. Open the python-telegram-bot service (NOT Postgres).
2. Settings -> Networking -> Generate Domain.
3. Redeploy/restart after the public domain exists.
4. Railway should provide RAILWAY_PUBLIC_DOMAIN automatically.
   Optional override: DASHBOARD_BASE_URL=https://your-domain.example

OPTIONAL VARIABLES
DASHBOARD_REFRESH_SECONDS=15
BETA_NOTICE_HOURS=12
ADMIN_USER_ID=<your Telegram numeric ID>

FILES TO UPLOAD/REPLACE
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py
bot/notice.py
bot/webapp.py   <-- new

No existing calls are deleted during deployment.
