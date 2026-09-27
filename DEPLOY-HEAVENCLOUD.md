# Deploying on HeavenCloud (control.heavencloud.in)

HeavenCloud runs a **Pterodactyl** panel, so this works like any Pterodactyl bot server.

## 1. Get a bot token (never paste it anywhere)

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it.
3. Turn on **Message Content Intent** (required for `!echo` / `!hello`).
4. **OAuth2 → URL Generator**: tick `bot`, give it **Send Messages** + **Read Message History**, open the link, invite the bot.

⚠️ The token is a password. Never send it in Discord chat, never commit it, never put it in `bot.py`. You do **not** need to give it to me to build the bot.

## 2. Create the server

1. Sign in at <https://control.heavencloud.in/create-server>.
2. Pick **Free Discord Bot** (or a paid plan), region **Nuremberg (Germany)** or **Miami (USA)**.
3. Submit. You get a server page with **Files / Console / Startup / Databases** tabs.

## 3. Upload the bot files

Quickest way — zip it locally first:

```powershell
Compress-Archive -Path bot.py, requirements.txt -DestinationPath celestial-bot.zip -Force
```

Then in the panel:

1. **Files** tab → **Upload** → pick `celestial-bot.zip`.
2. Click the zip → **Unarchive** → delete the zip.
3. You should now see `bot.py` and `requirements.txt` in `/home/container`.

(You can also drag-and-drop individual files, or use the **SFTP** details shown in the panel.)

## 4. Put the secrets in a `.env` file

The HeavenCloud Python egg only exposes `GIT_ADDRESS`, `BRANCH`, `USER_UPLOAD`
and `AUTO_UPDATE` — there is **no** token field. So the secrets go in a `.env`
file, which `bot.py` reads on boot and which you never commit anywhere.

1. Get a free OpenRouter key at <https://openrouter.ai/keys> (no card needed).
2. Panel → **Files** tab → **Create New File** → name it exactly `.env`.
3. Paste this in (fill in your own values):

   ```
   DISCORD_TOKEN=your-bot-token-here
   OPENROUTER_API_KEY=sk-or-your-key-here
   OPENROUTER_MODEL=openrouter/free
   ```

4. **Save**, then **Restart** the server.

⚠️ Get the token and key yourself from the Developer Portal and openrouter.ai/keys —
never paste either one into Discord, a ticket, or this chat.

Notes:

- `OPENROUTER_MODEL` is optional; without it the bot uses `openrouter/free`,
  which only routes to models that cost 0.
- Keep **USER_UPLOAD = Disabled (0)** — that's what makes the panel run
  `pip install -r requirements.txt`, and without it `discord.py` won't exist.
- `.env` lives on the panel's disk only. It is not in the zip you uploaded.
- If your free instance is ever wiped, re-create `.env` from your own backup.

## 5. Set the startup command

On the **Startup** tab, set the startup command to:

```
if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi; python3 -u bot.py
```

Some HeavenCloud Python eggs already run `pip install` during install — if so, `python3 -u bot.py` alone is enough.

## 6. Start it

Click **Start**. The **Console** tab should show:

```
Logged in as YourBot#1234 | servers: 1
------
```

Go to Discord and type `!ping`.

## 7. Renew it (free plan only)

Free servers must be **renewed every 7 days** in the panel or they suspend
(deleted after 2 more days). Renew early — free instances can also be wiped,
so keep your own copy of `bot.py` and your token.

## Troubleshooting

| Console / chat message | Fix |
| --- | --- |
| `DISCORD_TOKEN is not set` | No `.env` file (or wrong name) — create it in **Files**, then **Restart** |
| `LoginFailure: Improper token has been passed` | Token is wrong — reset it in the Developer Portal and paste it again |
| `OPENROUTER_API_KEY is not set` (from `!ai`) | Add the line to `.env`, then **Restart** |
| `OpenRouter rejected the API key` (401) | Key is wrong or was regenerated — paste the current one from openrouter.ai/keys |
| `Rate limited by OpenRouter` (429) | Free tier is strict — wait a minute, or change model with `!model qwen/qwen3.8-27b:free` |
| `Model … was not found` (404) | Use `!model openrouter/free`, or a live free model from openrouter.ai/models?max_price=0 |
| `403 Forbidden` on `!echo` | Enable **Message Content Intent** in Bot tab |
| `ModuleNotFoundError: discord` | Startup command must include the `pip install -r requirements.txt` part |
| Bot offline after 7 days | Free plan expired — renew in the panel |

## Rotating the token if you think it leaked

Developer Portal → your app → **Bot** → **Reset Token**. Every old copy of the
token stops working instantly. Then update `DISCORD_TOKEN` in the panel and restart.
