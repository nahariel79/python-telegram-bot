Stage 15 - Admin free-text broadcast

New command (private chat with the bot):
/broadcast

Security:
- Free-form broadcasting requires ADMIN_USER_ID to be set in Railway.
- If it is not set, /broadcast stays locked.
- Use /myid to see your Telegram user ID, then add ADMIN_USER_ID=<number> in Railway Variables.

Workflow:
1. Send /broadcast privately to the bot.
2. Send the exact text you want to publish, with Telegram formatting/emojis/links,
   OR send a photo with a caption.
3. The bot sends a preview.
4. Optional: add up to 6 URL buttons using: Button name | https://example.com
5. Choose:
   - Publish to channel
   - Publish and pin
   - Replace content
   - Cancel

Formatting/media:
- Telegram text entities are preserved by copying the original message.
- Photo + caption is supported.
- Public link buttons are copied to the channel post.
- Pinning requires the bot to have permission to pin messages in the channel.

No database schema changes are made by Stage 15.
Existing calls, ATH data, ranked lists and history remain unchanged.

Upload these files from this package:
main.py
bot/db.py
bot/handlers.py
bot/tracker.py
bot/stats.py

Only bot/handlers.py has functional changes from Stage 14; the full package is included to avoid version mixing.
