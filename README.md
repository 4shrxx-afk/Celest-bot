# Celestial Bot

Celestial — a Discord bot written in Python with [discord.py](https://discordpy.readthedocs.io/) and AI replies via **Mistral → OpenRouter → Groq** automatic failover chain.

## Commands

| Command | Description |
| --- | --- |
| `!celestial` | Who Celestial is + command list |
| `!ai <question>` | Ask anything (remembers the conversation) |
| `!chat <msg>` | Same shared memory — use either one |
| `!forget` | Clear chat memory |
| `!aiembed <idea>` | AI designs an embed for you |
| `!embed Title \| text \| #color` | Build an embed yourself |
| `!server` / `!user [@x]` / `!avatar [@x]` | Server + member info embeds |
| `!summarize [5-30]` | Recap recent chat |
| `!aishout [#channel] draft` | AI-polished @everyone announcement |
| `!notify add hi, help` | DM you when keywords appear |
| `!teach trigger \| reply` | Teach a custom auto-reply (`{user}`, `{server}` work) |
| `!unteach trigger` / `!teachlist` | Remove / list taught replies |
| `!teachmode me` / `!teachmode everyone` | Who triggers taught replies (default: only you) |
| `!remind 10m text` | Reminder delivered by DM |
| `!setavatar` + image | Change Celestial's profile picture |
| `!aicode <request>` | She writes discord.py code, delivered as a file |
| `!applysetup [#channel]` | Staff applications: button + form + accept/deny |
| `!ping` / `!hello` / `!echo text` | Basics |
| `!model` | Show all three provider models |
| `!model <mistral-id>` | Switch Mistral primary (e.g. `mistral-medium-latest`) |
| `!model openrouter\|groq <id>` | Switch that chain link |

Owner toolkit (needs `OWNER_ID`, see below):

`!nick @user NewName` · `!ainick @user <vibe>` · `!timeout @user 10m [reason]` ·
`!untimeout @user` · `!slowmode <s|off>` · `!lock` / `!unlock` ·
`!announce #channel Title \| text` · `!clear <1-30>` · `!aishout [#channel] draft` · `!notify add hi, help` ·
`!teach trigger \| reply` · `!remind 10m text` · `!setavatar` (with attached image)

Every command also works as a **/slash command** (type `/` in Discord).
Slash commands can take up to ~1 hour to appear after the first sync.

You can also just talk: `!ai rename @John to JD`, `!ai timeout @Sam 10m spam`,
`!ai shout game night friday`, `!ai make an embed about movie night`,
`!ai remind me in 10 minutes to check the oven`, `!ai summarize`.
Celestial does it herself (deletes still need the explicit `!clear`).

Attach an image to the same message as `!ai` / `!chat` and Celestial will look at it
(if the model supports vision — otherwise she answers text-only and says so).

## Lock it to yourself

Locked to you (`1341036065397411926`) automatically — no setup needed.
Anyone else running a command gets `🔒 Only my owner can use that.`
To add more owners: Render Dashboard → Environment → `OWNER_IDS=id1,id2` → redeploy.

Note: taught replies (`!teach`), keyword alerts (`!notify`) and chat memory
(`!ai`/`!chat`, last ~8 exchanges) live in memory —
a restart/redeploy clears them, so re-run those commands after updates.

## Hosting on Render + UptimeRobot

See **[DEPLOY-RENDER.md](DEPLOY-RENDER.md)** for the full step-by-step guide.
Short version:

1. Upload `bot.py`, `requirements.txt`, `render.yaml` to GitHub (no `.env`).
2. Render → New Web Service → connect the repo (Free plan).
3. Dashboard → Environment → `DISCORD_TOKEN`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL=openrouter/free`, `OWNER_ID`.
4. UptimeRobot → HTTP(s) monitor on your Render URL, every 5 min.
5. Test with `!ping` and `!ai who are you`.

The bot runs a tiny keep-alive web page on `$PORT` (stdlib only, no extra
deps) so Render's port check passes and UptimeRobot has something to ping.

## Permissions the bot needs

Re-invite via Developer Portal → OAuth2 → URL Generator (`bot` scope) with:
**Send Messages, Embed Links, Read Message History, Mention Everyone,
Manage Nicknames, Moderate Members, Manage Messages, Manage Channels.**
Put its role above anyone it should rename/timeout (it can never touch the server owner).
Portal → Bot → enable **Message Content Intent** + **Server Members Intent**.

## Security notes

- Secrets live in Render's **Environment** tab (or local `.env`) — never in `bot.py`.
- `.env` is gitignored — do not commit it.
- Only a **SHA-256 hash** of a user's Discord ID is sent to OpenRouter, never the raw ID. `OWNER_ID` never leaves Render.
- Never paste your bot token or API key into Discord messages, tickets, or a public repo.
- If a token ever leaks: **Reset Token** in the Developer Portal — old copies die instantly.
