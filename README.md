# Celestial Bot

A Discord bot written in Python with [discord.py](https://discordpy.readthedocs.io/) and AI replies via [OpenRouter](https://openrouter.ai).

## Commands

| Command                     | Description                                |
| --------------------------- | ------------------------------------------ |
| `!ai what is hello`         | Ask the AI (free models by default)        |
| `!model`                    | Show the current AI model                  |
| `!model qwen/qwen3.8-27b:free` | Switch model for this run                |
| `!ping`                     | Shows the bot's latency                    |
| `!hello`                    | Says hello                                 |
| `!echo text`                | Repeats the given text                     |

`!ai` is limited to 1 use per 5 seconds per user.

## Hosting on Render + UptimeRobot

See **[DEPLOY-RENDER.md](DEPLOY-RENDER.md)** for the full step-by-step guide.
Short version:

1. Upload `bot.py`, `requirements.txt`, `render.yaml` to GitHub (no `.env`).
2. Render → New Web Service → connect the repo (Free plan).
3. Dashboard → Environment → set `DISCORD_TOKEN`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL=openrouter/free`.
4. UptimeRobot → HTTP(s) monitor on your Render URL, every 5 min.
5. Test with `!ping` and `!ai what is hello`.

The bot runs a tiny keep-alive web page on `$PORT` (stdlib only, no extra
deps) so Render's port check passes and UptimeRobot has something to ping.

## Security notes

- Secrets live in Render's **Environment** tab (or local `.env`) — never in `bot.py`.
- `.env` is gitignored — do not commit it.
- The bot only sends a **SHA-256 hash** of a user's Discord ID to OpenRouter, never the raw ID.
- Never paste your bot token or API key into Discord messages, tickets, or a public repo.
- If a token ever leaks: **Reset Token** in the Developer Portal — old copies die instantly.
