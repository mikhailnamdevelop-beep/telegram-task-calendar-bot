# Telegram Task Calendar Bot

Text-only Telegram assistant. It parses task requests, keeps item state in Supabase Postgres, and creates bot-managed Google Calendar events. Events created outside the bot remain read-only.

## Local start

1. Create a Python 3.10+ virtual environment and install dependencies:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   Copy-Item .env.example .env
   ```

2. Fill in `.env`: create a bot with [@BotFather](https://t.me/BotFather), put its token in `TELEGRAM_BOT_TOKEN`, obtain your numeric Telegram user ID, and put it in `ALLOWED_USER_IDS`. The bot rejects every other message and callback.
3. Provision Supabase Postgres, apply the project's schema/migrations, and add the project URL plus service-role key as `SUPABASE_URL` and `SUPABASE_SECRET_KEY`. Treat the key as a deployment secret and never commit it.
4. In Google Cloud, create an OAuth Desktop client, enable Google Calendar API, and run `python scripts/google_oauth.py --client-secrets <untracked-json>`. Store the printed refresh token plus client values in `.env`. The bot reads external calendar events, but creates/edits/deletes only events that it owns (marked with its private item ID).
5. Start in polling mode:

   ```powershell
   python -m app.main
   ```

Use `/help` in Telegram. Plain text is treated as `/task`. Calendar and `/both` requests always show a Confirm/Cancel action; task-only requests are saved directly and offer Undo.

## Northflank webhook deployment

1. Build from the included `Dockerfile`; it runs as a non-root user and exposes port `8080`.
2. Add every `.env.example` value as a Northflank secret/environment variable. Set `BOT_MODE=webhook`, `WEBHOOK_BASE_URL` to the deployed public HTTPS URL (without the webhook path), and set a long random `TELEGRAM_WEBHOOK_SECRET`.
3. Configure the service health check as `GET /health` on port `8080`.
4. Deploy. At startup the app registers `<WEBHOOK_BASE_URL>/telegram/webhook` with Telegram. Setting `BOT_MODE=polling` switches safely back to local polling.

## Checks

```powershell
ruff check .
mypy --explicit-package-bases app scripts
pytest --cov=app --cov-fail-under=80
detect-secrets-hook --exclude-files '^tests/' --exclude-lines 'REPLACE_|placeholder|example\.com|db_key\s*=' $(git ls-files)
```

No real credentials, OAuth files, database exports, or tokens belong in Git.
