# Deploying on Render + UptimeRobot

Render's free **Web Service** sleeps after ~15 min with no web traffic.
`bot.py` already runs a tiny web server on `$PORT` that replies
`Celestial Bot is alive!` — UptimeRobot pings it every 5 min so Render
never idles out. No extra files or deps needed (stdlib only).

## 1. Get your two secrets (never paste them anywhere)

1. Bot token: <https://discord.com/developers/applications> → your app →
   **Bot → Reset Token** → copy. Also enable **Message Content Intent**.
2. AI key: <https://openrouter.ai/keys> → Create Key → copy (`sk-or-…`).

⚠️ These are passwords. Never send them in Discord, never commit them,
never paste them to anyone (including me).

## 2. Put the code on GitHub

Render deploys from a Git repo. Easiest way (no git needed locally):

1. Go to <https://github.com/new> → name it `celestial-bot` → **Public or Private** → Create.
2. Click **uploading an existing file** → drag in these files from this folder:
   - `bot.py`
   - `requirements.txt`
   - `render.yaml`
   - `.env.example` (reference only)
3. **Do NOT upload `.env`** (there isn't one here anyway). Commit.

## 3. Create the Render service

1. Sign up at <https://dashboard.render.com>.
2. **New + → Web Service → Build and deploy from a Git repository** →
   connect GitHub → pick `celestial-bot`.
3. Settings (most auto-fill from `render.yaml` — just verify):

   | Field | Value |
   | --- | --- |
   | Runtime | `Python 3` |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `python bot.py` |
   | Instance Type | `Free` |

4. Under **Environment → Environment Variables**, add:

   | Key | Value |
   | --- | --- |
   | `DISCORD_TOKEN` | your bot token |
   | `OPENROUTER_API_KEY` | your `sk-or-…` key |
   | `OPENROUTER_MODEL` | `openrouter/free` |
   | `PYTHON_VERSION` | `3.12.0` |

5. **Create Web Service**. Watch **Logs** — you should see:

   ```
   Logged in as YourBot#1234 | servers: 1 | model: openrouter/free
   ```

6. Copy your service URL, e.g. `https://celestial-bot-xxxx.onrender.com`.
   Open it in a browser — it should say `Celestial Bot is alive!`

## 4. Add UptimeRobot (keeps it awake 24/7)

1. Sign up at <https://uptimerobot.com> (free plan is enough).
2. **Add New Monitor**:

   | Field | Value |
   | --- | --- |
   | Monitor Type | `HTTP(s)` |
   | Friendly Name | `Celestial Bot` |
   | URL | your Render URL from step 3 |
   | Monitoring Interval | `5 Minutes` |

3. Create. Within ~10 min the monitor shows green/up.
4. Back in Discord, test: `!ping`, then `!ai what is hello`.

## 5. Updating the bot later

Push new commits to GitHub → Render **auto-redeploys**.
Secrets stay in Render's Environment tab — you never touch them again.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Logs: `DISCORD_TOKEN is not set` | Dashboard → Environment → add it → **Manual Deploy → Deploy latest commit** |
| `!ai` says key not set / 401 | `OPENROUTER_API_KEY` wrong — repaste from openrouter.ai/keys, redeploy |
| `LoginFailure: Improper token` | Token wrong — Reset Token in Developer Portal, update Render var |
| `429 Rate limited` from `!ai` | Free OpenRouter tier is strict — wait a minute, or `!model qwen/qwen3.8-27b:free` |
| `404 Model not found` | `!model openrouter/free` |
| `403 Forbidden` on `!ai`/`!echo` | Developer Portal → Bot → enable **Message Content Intent**, restart service |
| `ModuleNotFoundError: discord` | Build Command must be `pip install -r requirements.txt` |
| Service sleeps anyway | UptimeRobot interval must be ≤5 min and pointing at the root URL (`/`), not a subpath |
| First `!ai` after idle is slow | Normal cold start (~30–60s). Next commands are fast |
| Port / bind errors | Don't change the web-server code — Render injects `$PORT` itself |

## Rotating a leaked token/key

- Token: Developer Portal → Bot → **Reset Token** → update `DISCORD_TOKEN` in Render → redeploy.
- Key: openrouter.ai/keys → revoke + new key → update `OPENROUTER_API_KEY` → redeploy.
Old copies die instantly in both cases.
