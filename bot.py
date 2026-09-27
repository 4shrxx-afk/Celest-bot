import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# Loads .env for local testing only. On Render the values come from
# Dashboard -> Environment, so this import is optional.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ------------------------------------------------------- keep-alive web server
# Render free Web Services must bind to $PORT, and UptimeRobot pings this
# to stop Render spinning the service down after 15 min idle.
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Celestial Bot is alive!")

    def log_message(self, *args):
        return


def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        HTTPServer(("0.0.0.0", port), _Handler).serve_forever()
    except OSError as e:
        print(f"Web server could not bind port {port}: {e}", file=sys.stderr)


threading.Thread(target=start_web_server, daemon=True).start()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"  # free models only - costs 0

SYSTEM_PROMPT = (
    "You are Celestial, a friendly Discord bot living in the users' server. "
    "Your name is Celestial - always introduce yourself as Celestial, never as any model name. "
    "You are helpful, a little playful, and you know which server and channel you are in "
    "(that context is attached to each message). "
    "Answer clearly and keep replies short - a few sentences max, no markdown headers, "
    "unless the user asks for something long. "
    "Reply in the language the user wrote in. "
    "If the user attaches an image, look at it and describe or answer about what you see. "
    "If the user asks about this server, use the server context you were given. "
    "If anyone asks what model you are, always answer: you are Celestial, a custom model "
    "made by <@1341036065397411926> - never claim any other model name. "
    "You cannot change nicknames, timeouts or channels yourself - explicit bot commands do that. "
    "The owner can use: !nick @user NewName (rename), !ainick @user <vibe> (AI-styled rename), "
    "!timeout @user 10m [reason], !untimeout @user, !slowmode <seconds|off>, "
    "!lock / !unlock, !announce #channel Title | text, !clear <1-30>, "
    "!aishout <draft> (polished @everyone post), !notify add hi, help (DM you on keywords), "
    "!aiembed <idea> (you design an embed), !summarize [n]. "
    "If the owner asks in plain words, you DO it yourself (rename, timeout, slowmode, "
    "lock, announce, embed, summarize, remind). Only for message deletes, "
    "give the exact !clear command instead."
)

EMBED_JSON_PROMPT = (
    "You design beautiful, modern Discord embeds. Reply with ONLY a JSON object, "
    "no other text, no code fences. Schema: "
    '{"title": "short punchy title with one emoji, max 150 chars", '
    '"description": "well-formatted body with line breaks and a couple of emojis, max 2500 chars", '
    '"color": "a hex color that fits the mood (gold #FEE75C for parties, red #ED4245 for alerts, blurple #5865F2 default)", '
    '"fields": [{"name": "🎯 short name", "value": "short text", "inline": false}]} '
    "Fields are optional, max 4, each short. Keep everything tight and readable. "
    "Reply in the language the user wrote in."
)


def env(name: str) -> str:
    return os.getenv(name, "").strip().strip('"').strip("'")


def get_token() -> str:
    token = env("DISCORD_TOKEN")
    if not token or token == "your-bot-token-here":
        print(
            "ERROR: DISCORD_TOKEN is not set.\n"
            "  - Locally: copy .env.example to .env and paste your token.\n"
            "  - On Render: Dashboard -> Environment -> add DISCORD_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


TOKEN = get_token()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needs Server Members Intent in the Developer Portal

bot = commands.Bot(command_prefix="!", intents=intents)

# Current model. Can be swapped at runtime with !model <id>
current_model = env("OPENROUTER_MODEL") or DEFAULT_MODEL

# Conversational memory for !chat: (guild_id, user_id) -> last 8 turns
chat_history: dict = {}


# ---------------------------------------------------------------- owner lock
# Your Discord user id - the bot stays locked to you even if OWNER_ID is unset.
# OWNER_ID / OWNER_IDS env vars only ADD more owners.
DEFAULT_OWNER_IDS = {1341036065397411926}
OWNER_MENTION = "<@1341036065397411926>"


def get_owner_ids() -> set:
    """Owner id(s): built-in default plus OWNER_ID / OWNER_IDS env."""
    raw = env("OWNER_IDS") or env("OWNER_ID")
    ids = set(DEFAULT_OWNER_IDS)
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_owner_id(user_id: int) -> bool:
    ids = get_owner_ids()
    return (not ids) or (user_id in ids)


@bot.check
async def _global_owner_check(ctx: commands.Context) -> bool:
    """If OWNER_ID is set, nobody else can use any command."""
    if is_owner_id(ctx.author.id):
        return True
    raise commands.CheckFailure("Only the bot owner can use me.")


def check_privileged():
    """Moderation commands: owner-only once OWNER_ID is set, else server admins only."""
    async def predicate(ctx: commands.Context) -> bool:
        ids = get_owner_ids()
        if ids:
            if ctx.author.id in ids:
                return True
            raise commands.CheckFailure("Only the bot owner can use that.")
        if ctx.guild and (ctx.author.guild_permissions.administrator
                          or ctx.guild.owner_id == ctx.author.id):
            return True
        raise commands.CheckFailure("You need Administrator for that.")
    return commands.check(predicate)


# ------------------------------------------------------------------ OpenRouter
def anon_id(user_id: int) -> str:
    """Hash of the Discord user id - the raw id is never sent anywhere."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:32]


def server_context(ctx: commands.Context) -> str:
    """Short human-readable context injected into every AI request."""
    who = ctx.author.display_name
    if ctx.guild is None:
        return f"Direct-message chat with {who} (no server)."
    channel = getattr(ctx.channel, "name", "unknown-channel")
    members = ctx.guild.member_count or "unknown number of"
    return (
        f"You are in server '{ctx.guild.name}' with ~{members} members, "
        f"in channel #{channel}, talking to {who}."
    )


async def post_chat(session: aiohttp.ClientSession, messages: list,
                    user: str, max_tokens: int = 700,
                    temperature: float = 0.7):
    """POST a message list to OpenRouter. Returns (http_status, payload)."""
    headers = {
        "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
        "X-Title": "Celestial Bot",
    }
    body = {
        "model": current_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "user": user,
    }
    try:
        async with session.post(
            OPENROUTER_URL, json=body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            return resp.status, await resp.json(content_type=None)
    except aiohttp.ClientError:
        return 0, {"error": {"message": "Could not reach OpenRouter (network error)."}}
    except TimeoutError:
        return 0, {"error": {"message": "OpenRouter took too long to reply."}}


def extract_reply(payload: dict):
    try:
        text = payload["choices"][0]["message"]["content"].strip()
        return text or None
    except (KeyError, IndexError, AttributeError):
        return None


def error_message(status: int, payload: dict) -> str:
    msg = (payload.get("error") or {}).get("message") or "Unknown error."
    if status == 401:
        return "OpenRouter rejected the API key. Check `OPENROUTER_API_KEY`."
    if status == 402:
        return "Out of OpenRouter credits/quota for this model."
    if status == 404:
        return f"Model `{current_model}` was not found. Try `!model openrouter/free`."
    if status == 429:
        return "Rate limited by OpenRouter (free tier is strict). Try again in a minute."
    return f"OpenRouter error ({status}): {msg}"


async def send_long(ctx: commands.Context, text: str):
    """Send text split at Discord's 2000-char limit."""
    for i in range(0, len(text), 1990):
        await ctx.send(text[i:i + 1990])


def need_key(ctx: commands.Context) -> bool:
    return not env("OPENROUTER_API_KEY")


async def no_key_msg(ctx: commands.Context):
    await ctx.send(
        "`OPENROUTER_API_KEY` is not set - add it under Render "
        "Dashboard -> Environment (get a free key at openrouter.ai/keys)."
    )


def parse_duration(raw: str):
    """Parse '10s / 5m / 2h / 1d' into a timedelta. None if invalid."""
    m = re.fullmatch(
        r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)",
        (raw or "").strip().lower(),
    )
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("s"):
        return timedelta(seconds=n)
    if unit.startswith("m"):
        return timedelta(minutes=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    return timedelta(days=n)


def _pretty(embed: discord.Embed) -> discord.Embed:
    """One consistent look: timestamp always, Celestial footer unless custom."""
    try:
        embed.timestamp = discord.utils.utcnow()
    except Exception:
        pass
    try:
        if not (embed.footer and embed.footer.text):
            embed.set_footer(text="✨ Celestial")
    except Exception:
        pass
    return embed


_IDENTITY_TRIGGERS = (
    "who made you", "who created you", "who is your creator", "who is your owner",
    "what model are you", "which model are you", "what ai are you", "which ai are you",
    "what llm are you", "which llm are you", "are you chatgpt", "are you gpt",
    "are you claude", "are you gemini", "powered by",
)


def identity_reply(text: str):
    """Deterministic identity answer - no API call needed."""
    low = (text or "").lower()
    if any(t in low for t in _IDENTITY_TRIGGERS):
        return f"I'm **Celestial** ✨ — a custom model made by {OWNER_MENTION}."
    return None


def _msg_images(ctx: commands.Context) -> list:
    """Image blocks from the invoking message's attachments (prefix path)."""
    try:
        atts = ctx.message.attachments if ctx.message else []
    except Exception:
        atts = []
    parts = []
    for a in (atts or [])[:3]:
        try:
            if (a.content_type or "").startswith("image/") and a.size <= 10 * 1024 * 1024:
                parts.append({"type": "image_url", "image_url": {"url": a.url}})
        except Exception:
            continue
    return parts


def _user_content(text: str, images: list):
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


async def _run_ai(ctx: commands.Context, full_text: str, history: list = None,
                  max_tokens: int = 700, temperature: float = 0.7):
    """Shared AI call: identity intercept + vision fallback. Sends errors itself."""
    ident = identity_reply(full_text)
    if ident:
        return ident
    images = _msg_images(ctx)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\nLive context: " + server_context(ctx)},
        *(history or []),
        {"role": "user", "content": _user_content(full_text, images)},
    ]
    async with aiohttp.ClientSession() as session:
        status, payload = await post_chat(session, messages, anon_id(ctx.author.id),
                                          max_tokens=max_tokens, temperature=temperature)
        if status != 200 and images and status == 400:
            # Model can't see images - retry text-only once.
            messages[-1] = {"role": "user", "content": full_text}
            status, payload = await post_chat(session, messages, anon_id(ctx.author.id),
                                              max_tokens=max_tokens, temperature=temperature)
            if status == 200:
                reply = extract_reply(payload)
                if reply:
                    return "⚠️ *I couldn't view your image, answering text-only:*\n\n" + reply
        if status != 200:
            await ctx.send(error_message(status, payload))
            return None
        return extract_reply(payload)


async def _schedule_reminder(user_id: int, channel, seconds: float, text: str):
    await asyncio.sleep(seconds)
    embed = discord.Embed(title="⏰ Reminder", description=text[:1500], color=0xFEE75C)
    _pretty(embed)
    try:
        u = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if u is not None:
            try:
                await u.send(embed=_pretty(embed))
                return
            except (discord.Forbidden, discord.HTTPException):
                pass
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await channel.send(f"<@{user_id}> ⏰ Reminder: {text[:1500]}",
                           allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _maybe_reminder(ctx: commands.Context, text: str) -> bool:
    """Natural 'remind me in X to Y' handling. True if handled."""
    m = re.fullmatch(r"\s*remind me in (.+?) to (.+)", (text or "").strip(),
                     re.IGNORECASE | re.DOTALL)
    if not m:
        return False
    dur = parse_duration(m.group(1))
    what = m.group(2).strip()
    if dur is None or not what:
        return False
    secs = dur.total_seconds()
    if secs < 10 or secs > 86400:
        await ctx.send("I can remind you between 10 seconds and 24 hours. (Restarts clear pending reminders.)")
        return True
    asyncio.create_task(_schedule_reminder(ctx.author.id, ctx.channel, secs, what))
    await ctx.send(f"⏰ Got it — I'll remind you in **{m.group(1).strip()}**: {what[:200]}")
    return True


def _action_allowed(ctx: commands.Context) -> bool:
    if ctx.author.id in get_owner_ids():
        return True
    if ctx.guild and (ctx.author.guild_permissions.administrator
                      or ctx.guild.owner_id == ctx.author.id):
        return True
    return False


async def _resolve_member(ctx: commands.Context, hint: str):
    """@mention first, then display-name / username lookup ('me' = yourself)."""
    if (hint or "").strip().lower() in ("me", "myself"):
        return ctx.author
    try:
        mentions = ctx.message.mentions if ctx.message else []
    except Exception:
        mentions = []
    for m in mentions or []:
        if isinstance(m, discord.Member):
            return m
    if ctx.guild is None:
        return None
    hint = (hint or "").strip().lstrip("@").lower()
    if not hint:
        return None
    members = ctx.guild.members
    for m in members:
        if m.display_name.lower() == hint or m.name.lower() == hint:
            return m
    for m in members:
        if m.display_name.lower().startswith(hint) or m.name.lower().startswith(hint):
            return m
    return None


async def _maybe_action(ctx: commands.Context, text: str) -> bool:
    """Plain-words execution for privileged users. True if handled."""
    if ctx.guild is None or not _action_allowed(ctx):
        return False
    low = (text or "").strip()
    if not low:
        return False
    pls = r"(?:please\s+)?"

    # -- rename: "rename X to Y" / "change X's name to Y" / "call X Y" / "rename me to Y"
    m = (re.fullmatch(pls + r"rename me\s+to\s+(.+)", low, re.IGNORECASE | re.DOTALL)
         or re.fullmatch(pls + r"(?:change|set) my name\s+to\s+(.+)", low, re.IGNORECASE | re.DOTALL))
    if m:
        member, new_name = ctx.author, m.group(1).strip().strip("'\"")
    else:
        m = (re.fullmatch(pls + r"rename\s+(.+?)\s+to\s+(.+)", low, re.IGNORECASE | re.DOTALL)
             or re.fullmatch(pls + r"change\s+(.+?)'?s?\s+name\s+to\s+(.+)", low, re.IGNORECASE | re.DOTALL)
             or re.fullmatch(pls + r"call\s+(.+?)\s+(\S(?:.+)?)", low, re.IGNORECASE | re.DOTALL))
        member, new_name = None, None
        if m:
            member = await _resolve_member(ctx, m.group(1))
            new_name = m.group(2).strip().strip("'\"")
    if m:
        if member is None:
            await ctx.send("I couldn't find that person — mention them or check the spelling.")
            return True
        if not new_name or len(new_name) > 32:
            await ctx.send("New name must be 1–32 characters.")
            return True
        try:
            await member.edit(nick=new_name, reason=f"Renamed by {ctx.author} via Celestial")
        except discord.Forbidden:
            await ctx.send("I can't rename them (need **Manage Nicknames** + higher role).")
            return True
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that rename: {e}")
            return True
        await ctx.send(f"Renamed {member.mention} to **{new_name}**.")
        return True

    # -- timeout: "timeout X 10m reason" / "mute X 10m"
    m = re.fullmatch(
        pls + r"(?:timeout|mute)\s+(.+?)\s+(?:for\s+)?(\d+\s*(?:s|sec|secs|m|min|mins|h|hour|hours|d|day|days))"
        r"(?:\s+(?:for\s+|because\s+|reason:?\s+)?(.+))?",
        low, re.IGNORECASE | re.DOTALL)
    if m:
        member = await _resolve_member(ctx, m.group(1))
        if member is None:
            await ctx.send("I couldn't find that person — mention them or check the spelling.")
            return True
        dur = parse_duration(m.group(2))
        reason = (m.group(3) or "No reason").strip()[:200]
        if dur is None or dur.total_seconds() < 5:
            await ctx.send("I need a duration like `10s`, `10m`, `2h`, `1d` (max 28d).")
            return True
        if dur > timedelta(days=28):
            dur = timedelta(days=28)
        try:
            await member.timeout(dur, reason=f"{reason} (by {ctx.author} via Celestial)")
        except discord.Forbidden:
            await ctx.send("I can't timeout them (need **Moderate Members** + higher role).")
            return True
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that timeout: {e}")
            return True
        await ctx.send(f"⏳ {member.mention} timed out. Reason: {reason}")
        return True

    # -- untimeout: "untimeout X" / "unmute X"
    m = re.fullmatch(pls + r"(?:untimeout|unmute|remove (?:the )?timeout (?:for|from|on))\s+(.+)",
                     low, re.IGNORECASE | re.DOTALL)
    if m:
        member = await _resolve_member(ctx, m.group(1))
        if member is None:
            await ctx.send("I couldn't find that person — mention them or check the spelling.")
            return True
        try:
            await member.timeout(None, reason=f"Untimeout by {ctx.author} via Celestial")
        except discord.Forbidden:
            await ctx.send("I can't untimeout them (need **Moderate Members**).")
            return True
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that: {e}")
            return True
        await ctx.send(f"✅ {member.mention} is free to chat again.")
        return True

    # -- slowmode: "slowmode 10" / "set slowmode to 1m" / "slowmode off" / "turn off slowmode"
    if re.fullmatch(pls + r"turn off slowmode", low, re.IGNORECASE):
        low = "slowmode off"
    m = re.fullmatch(pls + r"(?:set\s+|turn\s+on\s+)?slowmode\s+(?:to\s+)?(off|\d+\s*(?:s|sec|secs|m|min|mins|h|hour|hours)?)",
                     low, re.IGNORECASE)
    if m:
        if not hasattr(ctx.channel, "edit"):
            await ctx.send("Use that in a server text channel.")
            return True
        val = m.group(1).strip().lower()
        if val == "off":
            seconds = 0
        else:
            dur = parse_duration(val)
            if dur is not None:
                seconds = int(dur.total_seconds())
            else:
                try:
                    seconds = int(re.fullmatch(r"\d+", val).group(0))
                except (ValueError, AttributeError):
                    return False
        seconds = max(0, min(21600, seconds))
        try:
            await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author} via Celestial")
        except discord.Forbidden:
            await ctx.send("I can't edit this channel (need **Manage Channels**).")
            return True
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that: {e}")
            return True
        await ctx.send("🐢 Slowmode off." if seconds == 0 else f"🐢 Slowmode set to **{seconds}s**.")
        return True

    # -- lock / unlock this channel
    if re.fullmatch(pls + r"unlock\s+(?:this\s+|the\s+)?channel|unlock it", low, re.IGNORECASE):
        try:
            await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None,
                                              reason=f"Unlocked by {ctx.author} via Celestial")
        except discord.Forbidden:
            await ctx.send("I can't unlock this (need **Manage Channels** / **Manage Roles**).")
            return True
        await ctx.send("🔓 Channel unlocked.")
        return True
    if re.fullmatch(pls + r"lock\s+(?:this\s+|the\s+)?channel|lock it|lockdown", low, re.IGNORECASE):
        try:
            await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False,
                                              reason=f"Locked by {ctx.author} via Celestial")
        except discord.Forbidden:
            await ctx.send("I can't lock this (need **Manage Channels** / **Manage Roles**).")
            return True
        await ctx.send("🔒 Channel locked.")
        return True

    # -- shout / announce: "shout game night friday" / "announce in #news X"
    m = re.fullmatch(pls + r"(shout|broadcast|ping everyone|announce)(?:\s+in\s+(\S+))?[\s:]+(.+)",
                     low, re.IGNORECASE | re.DOTALL)
    if m:
        verb, chname, body = m.group(1).lower(), m.group(2), m.group(3).strip()
        try:
            ch_mentions = ctx.message.channel_mentions if ctx.message else []
        except Exception:
            ch_mentions = []
        target = ctx.channel
        if ch_mentions:
            target = ch_mentions[0]
            body = body.replace(target.mention, "").strip()
        elif chname:
            found = discord.utils.get(ctx.guild.text_channels, name=chname.lstrip("#").lower())
            if found is None:
                await ctx.send(f"I can't find channel **{chname}**.")
                return True
            target = found
        if len(body) < 3:
            return False
        ping_all = verb != "announce" or "everyone" in body.lower()
        if ping_all:
            if need_key(ctx):
                await no_key_msg(ctx)
                return True
            messages = [
                {"role": "system", "content": (
                    "You polish Discord server announcements. Rewrite the draft to be clear, "
                    "energetic and short (under 1200 characters). Keep every fact, date and time "
                    "exactly as given. No markdown headers. Same language as the draft. "
                    "Reply ONLY with the announcement text. Live context: " + server_context(ctx)
                )},
                {"role": "user", "content": body},
            ]
            async with ctx.typing():
                async with aiohttp.ClientSession() as session:
                    status, payload = await post_chat(session, messages, anon_id(ctx.author.id),
                                                      max_tokens=500, temperature=0.7)
            if status != 200:
                await ctx.send(error_message(status, payload))
                return True
            polished = (extract_reply(payload) or "").strip() or body
            try:
                await target.send("@everyone\n\n" + polished[:1900],
                                  allowed_mentions=discord.AllowedMentions(everyone=True))
            except discord.Forbidden:
                await ctx.send(f"I can't post in {target.mention} (need Send Messages + Mention Everyone).")
                return True
            except discord.HTTPException as e:
                await ctx.send(f"Discord refused that post: {e}")
                return True
            if target.id != ctx.channel.id:
                await ctx.send(f"📢 Posted in {target.mention}.")
            return True
        embed = discord.Embed(title="📢 Announcement", description=body[:4096], color=0xFEE75C)
        embed.set_footer(text=f"Announced by {ctx.author.display_name}")
        try:
            await target.send(embed=_pretty(embed))
        except discord.Forbidden:
            await ctx.send(f"I can't post in {target.mention}.")
            return True
        if target.id != ctx.channel.id:
            await ctx.send(f"📢 Posted in {target.mention}.")
        return True

    # -- embed: "make an embed about game night"
    m = re.fullmatch(pls + r"make\s+(?:an?\s+)?embed\s+(?:about\s+|saying\s+|with\s+|that\s+says\s+|:\s*)?(.+)",
                     low, re.IGNORECASE | re.DOTALL)
    if m:
        prompt = m.group(1).strip()
        if len(prompt) < 3:
            return False
        if need_key(ctx):
            await no_key_msg(ctx)
            return True
        messages = [
            {"role": "system", "content": EMBED_JSON_PROMPT + " Live context: " + server_context(ctx)},
            {"role": "user", "content": prompt},
        ]
        async with ctx.typing():
            async with aiohttp.ClientSession() as session:
                status, payload = await post_chat(session, messages, anon_id(ctx.author.id),
                                                  max_tokens=900, temperature=0.7)
        if status != 200:
            await ctx.send(error_message(status, payload))
            return True
        raw = (extract_reply(payload) or "").strip()
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        try:
            data = json.loads(cleaned)
            embed = discord.Embed(
                title=str(data.get("title", "Celestial"))[:256],
                description=str(data.get("description", ""))[:4096],
                color=_parse_color(str(data.get("color", "#9B59B6"))),
            )
            for f in (data.get("fields") or [])[:4]:
                try:
                    embed.add_field(name=str(f.get("name", "•"))[:256],
                                    value=str(f.get("value", "—"))[:1024],
                                    inline=bool(f.get("inline", False)))
                except (AttributeError, TypeError):
                    continue
            await ctx.send(embed=_pretty(embed))
        except (json.JSONDecodeError, AttributeError, TypeError):
            await ctx.send("I couldn't shape that as an embed, here's the text version:")
            await send_long(ctx, raw)
        return True

    # -- server info: "server info" / "about this server"
    if re.fullmatch(pls + r"(?:server info|server stats|about this server|show (?:me )?the server info|this server'?s? info)",
                    low, re.IGNORECASE):
        await ctx.send(embed=_pretty(_server_embed(ctx.guild)))
        return True

    # -- user info: "who is X" (only when X is a member, else normal AI answer)
    m = re.fullmatch(pls + r"(?:who is|user info(?: about| for| on)?|info about|tell me about)\s+(.+)",
                     low, re.IGNORECASE | re.DOTALL)
    if m:
        member = await _resolve_member(ctx, m.group(1))
        if member is None:
            return False
        await ctx.send(embed=_pretty(_user_embed(member)))
        return True

    # -- summarize: "summarize" / "recap" / "catch me up" / "summarize last 10"
    m = re.fullmatch(pls + r"(?:summarize|summary|recap|catch me up)(?:\s+(?:the\s+)?chat)?(?:\s+last\s+(\d+))?",
                     low, re.IGNORECASE)
    if m:
        if need_key(ctx):
            await no_key_msg(ctx)
            return True
        try:
            await _do_summarize(ctx, int(m.group(1)) if m.group(1) else 20)
        except ValueError:
            await _do_summarize(ctx, 20)
        return True

    return False


# -------------------------------------------------------------------- commands
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | servers: {len(bot.guilds)} | model: {current_model}")
    print(f"Owner lock: {sorted(get_owner_ids())}")
    print("------")
    try:
        await bot.change_presence(activity=discord.Game(name="!ai | Celestial"))
    except Exception:
        pass
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print(f"Slash sync failed: {e}")


@bot.hybrid_command(name="ping", description="Check the bot's latency")
async def ping(ctx: commands.Context):
    """Replies with the bot's latency."""
    await ctx.send(f"Pong! {bot.latency * 1000:.0f} ms")


@bot.hybrid_command(name="hello", description="Say hello to Celestial")
async def hello(ctx: commands.Context):
    """Says hello."""
    await ctx.send(f"Hello {ctx.author.display_name}! I am **Celestial**. ✨")


@bot.hybrid_command(name="echo", description="Repeat your text back")
async def echo(ctx: commands.Context, *, text: str):
    """Repeats whatever you type after !echo."""
    await ctx.send(text)


@bot.hybrid_command(name="celestial", description="Who Celestial is + command list")
async def celestial_cmd(ctx: commands.Context):
    """Intro embed: who Celestial is and what she can do."""
    embed = discord.Embed(
        title="✨ I am Celestial",
        description=(
            "Your server's AI companion. Ask me anything with `!ai`, "
            "chat with me via `!chat`, or let me build embeds and summaries."
        ),
        color=0x9B59B6,
    )
    embed.add_field(
        name="🤖 AI",
        value="`!ai <question>` — one-shot answer\n`!chat <msg>` — talk (I remember)\n`!forget` — clear memory\nPlain words work too — just tell me what to do",
        inline=False,
    )
    embed.add_field(
        name="🎨 Embeds",
        value="`!aiembed <idea>` — I design the embed\n`!embed Title | text | #color` — you design it",
        inline=False,
    )
    embed.add_field(
        name="📊 Server",
        value="`!server` — this server\n`!user [@someone]` — member info\n`!avatar [@someone]` — avatar\n`!summarize [n]` — recap chat",
        inline=False,
    )
    embed.add_field(
        name="🔔 Smart alerts + shouts",
        value="`!notify add hi, help` — DM you on keywords\n`!aishout <draft>` — AI-polished @everyone post",
        inline=False,
    )
    embed.add_field(
        name="🔒 Owner toolkit",
        value="`!nick` · `!ainick` · `!timeout` · `!untimeout` · `!slowmode` · `!lock` · `!unlock` · `!announce` · `!clear`",
        inline=False,
    )
    await ctx.send(embed=_pretty(embed))


@bot.hybrid_command(name="ai", description="Ask Celestial anything")
@commands.cooldown(rate=1, per=5.0, type=commands.BucketType.user)
async def ai(ctx: commands.Context, *, prompt: str):
    """Ask Celestial anything. Usage: !ai what is hello"""
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    if await _maybe_reminder(ctx, prompt):
        return
    if await _maybe_action(ctx, prompt):
        return
    async with ctx.typing():
        reply = await _run_ai(ctx, prompt)
    if not reply:
        await ctx.send("The model returned an empty reply. Try rephrasing.")
        return
    await send_long(ctx, reply)


@bot.hybrid_command(name="chat", description="Chat with Celestial (she remembers)")
@commands.cooldown(rate=1, per=5.0, type=commands.BucketType.user)
async def chat(ctx: commands.Context, *, msg: str):
    """Chat with Celestial - she remembers the last 8 turns. Usage: !chat hi!"""
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    if await _maybe_reminder(ctx, msg):
        return
    if await _maybe_action(ctx, msg):
        return
    guild_id = ctx.guild.id if ctx.guild else 0
    key = (guild_id, ctx.author.id)
    history = chat_history.setdefault(key, deque(maxlen=8))
    async with ctx.typing():
        reply = await _run_ai(ctx, msg, history=list(history))
    if not reply:
        await ctx.send("The model returned an empty reply. Try rephrasing.")
        return
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply[:1500]})
    await send_long(ctx, reply)


@bot.hybrid_command(name="forget", description="Clear Celestial's chat memory")
async def forget(ctx: commands.Context):
    """Clear Celestial's memory of you. Usage: !forget"""
    guild_id = ctx.guild.id if ctx.guild else 0
    chat_history.pop((guild_id, ctx.author.id), None)
    await ctx.send("Memory cleared. Fresh start! ✨")


def _server_embed(g: discord.Guild) -> discord.Embed:
    embed = discord.Embed(title=f"📊 {g.name}", color=0x5865F2)
    try:
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
    except Exception:
        pass
    owner = getattr(g.owner, "display_name", "—") if g.owner else "—"
    embed.add_field(name="Owner", value=str(owner), inline=True)
    embed.add_field(name="Members", value=str(g.member_count or "—"), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
    embed.add_field(name="Text channels", value=str(len(g.text_channels)), inline=True)
    embed.add_field(name="Voice channels", value=str(len(g.voice_channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    return embed


def _user_embed(member: discord.Member) -> discord.Embed:
    embed = discord.Embed(title=f"👤 {member.display_name}", color=0x57F287)
    try:
        embed.set_thumbnail(url=member.display_avatar.url)
    except Exception:
        pass
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"), inline=True)
    try:
        if member.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "D"), inline=True)
    except Exception:
        pass
    try:
        roles = [r.name for r in member.roles if r.name != "@everyone"][:5]
        if roles:
            embed.add_field(name="Roles", value=", ".join(roles), inline=False)
    except Exception:
        pass
    return embed


@bot.hybrid_command(name="server", description="Show info about this server")
async def server_cmd(ctx: commands.Context):
    """Show info about this server."""
    if ctx.guild is None:
        await ctx.send("We're in DMs - there is no server here.")
        return
    await ctx.send(embed=_pretty(_server_embed(ctx.guild)))


@bot.hybrid_command(name="user", description="Show info about a member")
async def user_cmd(ctx: commands.Context, member: discord.Member = None):
    """Show info about you or someone. Usage: !user [@someone]"""
    member = member or ctx.author
    await ctx.send(embed=_pretty(_user_embed(member)))


@bot.hybrid_command(name="avatar", description="Show a member's avatar")
async def avatar_cmd(ctx: commands.Context, member: discord.Member = None):
    """Show someone's avatar big. Usage: !avatar [@someone]"""
    member = member or ctx.author
    embed = discord.Embed(title=f"🖼️ {member.display_name}", color=0x5865F2)
    try:
        embed.set_image(url=member.display_avatar.url)
    except Exception:
        await ctx.send("Could not load that avatar.")
        return
    await ctx.send(embed=_pretty(embed))


def _parse_color(raw: str) -> int:
    names = {
        "blurple": 0x5865F2, "red": 0xED4245, "green": 0x57F287,
        "gold": 0xFEE75C, "yellow": 0xFEE75C, "purple": 0x9B59B6,
        "pink": 0xEB459E, "orange": 0xE67E22, "grey": 0x95A5A6, "gray": 0x95A5A6,
    }
    s = (raw or "").strip().lower().lstrip("#")
    if s in names:
        return names[s]
    try:
        return int(s, 16)
    except ValueError:
        return 0x5865F2


@bot.hybrid_command(name="embed", description="Build a rich embed")
async def embed_cmd(ctx: commands.Context, *, text: str):
    """Build an embed yourself. Usage: !embed Title | description | #9B59B6"""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 2:
        await ctx.send("Usage: `!embed Title | description | #color (optional)`")
        return
    title, description = parts[0][:256], parts[1][:4096]
    color = _parse_color(parts[2]) if len(parts) >= 3 and parts[2] else 0x5865F2
    await ctx.send(embed=discord.Embed(title=title, description=description, color=color))


@bot.hybrid_command(name="aiembed", description="Let Celestial design an embed")
@commands.cooldown(rate=1, per=10.0, type=commands.BucketType.user)
async def aiembed(ctx: commands.Context, *, prompt: str):
    """Celestial designs an embed for you. Usage: !aiembed announcement for game night friday"""
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    messages = [
        {"role": "system", "content": EMBED_JSON_PROMPT + " Live context: " + server_context(ctx)},
        {"role": "user", "content": prompt},
    ]
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            status, payload = await post_chat(
                session, messages, anon_id(ctx.author.id),
                max_tokens=900, temperature=0.7,
            )
    if status != 200:
        await ctx.send(error_message(status, payload))
        return
    raw = extract_reply(payload)
    if not raw:
        await ctx.send("The model returned an empty reply. Try rephrasing.")
        return
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
        embed = discord.Embed(
            title=str(data.get("title", "Celestial"))[:256],
            description=str(data.get("description", ""))[:4096],
            color=_parse_color(str(data.get("color", "#9B59B6"))),
        )
        for f in (data.get("fields") or [])[:4]:
            try:
                embed.add_field(
                    name=str(f.get("name", "•"))[:256],
                    value=str(f.get("value", "—"))[:1024],
                    inline=bool(f.get("inline", False)),
                )
            except (AttributeError, TypeError):
                continue
        await ctx.send(embed=_pretty(embed))
    except (json.JSONDecodeError, AttributeError, TypeError):
        await ctx.send("I couldn't shape that as an embed, here's the text version:")
        await send_long(ctx, raw)


async def _collect_transcript(channel, skip_id, limit: int):
    """Last messages as 'Name: text' lines (newest last). None if unreadable."""
    lines = []
    try:
        async for m in channel.history(limit=limit + 1):
            if (skip_id is not None and m.id == skip_id) or not m.content.strip():
                continue
            if m.content.startswith("!") or m.content.startswith("/"):
                continue
            lines.append(f"{m.author.display_name}: {m.content[:300]}")
    except discord.Forbidden:
        return None
    return lines[::-1]


async def _do_summarize(ctx: commands.Context, limit: int):
    """Fetch + summarize + send. Assumes the API key was already checked."""
    limit = max(5, min(30, limit))
    cmd_id = ctx.message.id if ctx.message else None
    lines = await _collect_transcript(ctx.channel, cmd_id, limit)
    if lines is None:
        await ctx.send("I can't read message history here.")
        return
    if len(lines) < 2:
        await ctx.send("Not enough chat to summarize yet.")
        return
    transcript = "\n".join(lines)[:6000]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + " Summarize conversations briefly with bullet points."},
        {"role": "user", "content": f"Summarize this Discord chat from #{getattr(ctx.channel, 'name', 'chat')}:\n\n{transcript}"},
    ]
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            status, payload = await post_chat(session, messages, anon_id(ctx.author.id))
    if status != 200:
        await ctx.send(error_message(status, payload))
        return
    reply = extract_reply(payload)
    if not reply:
        await ctx.send("Nothing to summarize.")
        return
    embed = discord.Embed(title=f"📝 Recap of last {len(lines)} messages",
                          description=reply[:4096], color=0xFEE75C)
    await ctx.send(embed=_pretty(embed))


@bot.hybrid_command(name="summarize", description="Recap recent chat")
@commands.cooldown(rate=1, per=30.0, type=commands.BucketType.channel)
async def summarize(ctx: commands.Context, limit: int = 20):
    """Recap recent chat. Usage: !summarize [5-30]"""
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    await _do_summarize(ctx, limit)


# --------------------------------------------- smart alerts + shouts
# Keyword watch: DM the owner when watched words appear. In-memory only -
# a restart/redeploy clears the list (set it again with !notify add ...).
watch_keywords: dict = {}
notify_cooldown: dict = {}


@bot.listen("on_message")
async def _notify_watch(message: discord.Message):
    try:
        if message.author.bot or message.guild is None:
            return
        oids = get_owner_ids()
        if not oids or message.author.id in oids:
            return
        kws = watch_keywords.get(message.guild.id)
        if not kws:
            return
        low = (message.content or "").lower()
        if not low:
            return
        hit = None
        for kw in kws:
            kw = (kw or "").strip().lower()
            if not kw:
                continue
            if re.search(r"\b" + r"\s+".join(map(re.escape, kw.split())) + r"\b", low):
                hit = kw
                break
        if hit is None:
            return
        now = time.monotonic()
        key = (message.guild.id, message.author.id, hit)
        if now - notify_cooldown.get(key, 0) < 900:
            return
        if now - notify_cooldown.get((message.guild.id, "_g"), 0) < 120:
            return
        notify_cooldown[key] = now
        notify_cooldown[(message.guild.id, "_g")] = now
        embed = discord.Embed(
            title="🔔 Keyword alert",
            description=(message.content[:1000] or "(no text)"),
            color=0xEB459E,
        )
        embed.add_field(name="From", value=message.author.display_name, inline=True)
        embed.add_field(name="Channel", value=f"#{getattr(message.channel, 'name', 'chat')}", inline=True)
        embed.add_field(name="Server", value=message.guild.name, inline=True)
        embed.add_field(name="Jump to message", value=f"[Open]({message.jump_url})", inline=False)
        for oid in oids:
            try:
                u = bot.get_user(oid) or await bot.fetch_user(oid)
                if u is not None:
                    await u.send(embed=_pretty(embed))
            except (discord.Forbidden, discord.HTTPException):
                continue
    except Exception:
        pass


@bot.hybrid_command(name="notify", description="Keyword DM alerts (owner only)")
@check_privileged()
async def notify_cmd(ctx: commands.Context, action: str = "list", *, text: str = ""):
    """DM yourself on keywords. Usage: !notify add hi, help | !notify list | !notify remove hi | !notify clear"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    if not get_owner_ids():
        await ctx.send("Set `OWNER_ID` in Render → Environment first - otherwise I don't know who to DM.")
        return
    current = watch_keywords.setdefault(ctx.guild.id, set())
    action = (action or "list").strip().lower()
    if action == "add":
        words = {w.strip().lower() for w in text.split(",") if w.strip()}
        words = {w for w in words if len(w) <= 40}
        if not words:
            await ctx.send("Usage: `!notify add hi, help, game night`")
            return
        current |= words
        await ctx.send(
            f"👀 Watching: {', '.join(sorted(current))}\n"
            "I'll DM you (max 1 per person per 15 min). ⚠️ Common words like `hi` = lots of DMs."
        )
    elif action in ("remove", "rm", "del", "delete"):
        words = {w.strip().lower() for w in text.split(",") if w.strip()}
        current -= words
        await ctx.send(f"👀 Watching: {', '.join(sorted(current)) or '(nothing)'}")
    elif action == "clear":
        current.clear()
        await ctx.send("🔕 Watch list cleared.")
    else:
        await ctx.send(
            f"👀 Watching: {', '.join(sorted(current)) or '(nothing)'}\n"
            "Usage: `!notify add hi, help` · `!notify remove hi` · `!notify clear`"
        )


# ------------------------------------------------- teachable replies
# Owner-only training, auto-replies for everyone. In-memory: restarts clear it.
taught: dict = {}


@bot.listen("on_message")
async def _teach_reply(message: discord.Message):
    try:
        if message.author.bot:
            return
        if (message.content or "").startswith("!"):
            return
        gid = message.guild.id if message.guild else 0
        pairs = taught.get(gid)
        if not pairs:
            return
        if not _teach_allowed(message):
            return
        resp = pairs.get(message.content.strip().lower())
        if not resp:
            return
        now = time.monotonic()
        ck = (gid, message.content.strip().lower(), message.channel.id)
        if now - notify_cooldown.get(ck, 0) < 5:
            return
        notify_cooldown[ck] = now
        out = resp.replace("{user}", message.author.mention).replace(
            "{server}", message.guild.name if message.guild else "DM")
        await message.channel.send(out[:1500])
    except Exception:
        pass


@bot.hybrid_command(name="teach", description="Teach Celestial a custom reply (owner only)")
@check_privileged()
async def teach_cmd(ctx: commands.Context, *, text: str):
    """Teach a reply. Usage: !teach hello there | Hi {user}, welcome to {server}!"""
    parts = [p.strip() for p in text.split("|", 1)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await ctx.send("Usage: `!teach <trigger> | <reply>` — e.g. `!teach goodnight | Sweet dreams {user}! 🌙`")
        return
    trig, resp = parts[0].lower()[:100], parts[1][:1500]
    if trig.startswith("!"):
        await ctx.send("Triggers can't start with `!` (that's for commands).")
        return
    gid = ctx.guild.id if ctx.guild else 0
    taught.setdefault(gid, {})[trig] = resp
    await ctx.send(f"📚 Learned! When someone says **{trig}**, I'll reply.")


@bot.hybrid_command(name="unteach", description="Remove a taught reply (owner only)")
@check_privileged()
async def unteach_cmd(ctx: commands.Context, *, trigger: str):
    """Remove a taught reply. Usage: !unteach hello there"""
    gid = ctx.guild.id if ctx.guild else 0
    pairs = taught.get(gid, {})
    if pairs.pop(trigger.strip().lower(), None) is None:
        await ctx.send("I don't know that one.")
    else:
        await ctx.send(f"🗑️ Forgot **{trigger.strip()}**.")


@bot.hybrid_command(name="teachlist", description="List taught replies (owner only)")
@check_privileged()
async def teachlist_cmd(ctx: commands.Context):
    """List everything taught. Usage: !teachlist"""
    gid = ctx.guild.id if ctx.guild else 0
    pairs = taught.get(gid, {})
    if not pairs:
        await ctx.send("Nothing taught yet. Use `!teach <trigger> | <reply>`.")
    else:
        await ctx.send("📚 Taught replies: " + ", ".join(f"**{t}**" for t in sorted(pairs)))


# Who taught replies answer: "me" (default, owner only) or "everyone".
teach_mode: dict = {}


def _teach_allowed(message: discord.Message) -> bool:
    if message.guild is None:
        return message.author.id in get_owner_ids()
    if teach_mode.get(message.guild.id, "me") == "everyone":
        return True
    return message.author.id in get_owner_ids()


@bot.hybrid_command(name="teachmode", description="Who triggers taught replies (owner only)")
@check_privileged()
async def teachmode_cmd(ctx: commands.Context, mode: str = ""):
    """Set trigger scope. Usage: !teachmode me | !teachmode everyone"""
    gid = ctx.guild.id if ctx.guild else 0
    mode = (mode or "").strip().lower()
    if mode in ("me", "owner", "only me", "just me"):
        teach_mode[gid] = "me"
        await ctx.send("📚 Taught replies now answer **only you**.")
    elif mode in ("everyone", "all", "anyone", "everybody"):
        teach_mode[gid] = "everyone"
        await ctx.send("📚 Taught replies now answer **everyone**.")
    else:
        cur = teach_mode.get(gid, "me")
        await ctx.send(f"Currently: **{cur}**. Usage: `!teachmode me` or `!teachmode everyone`.")


# ------------------------------------------------------- owner toolkit
@bot.hybrid_command(name="nick", description="Rename a member (owner only)")
@check_privileged()
async def nick_cmd(ctx: commands.Context, member: discord.Member, *, new_name: str):
    """Rename someone. Usage: !nick @user New Name | !nick @user clear"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    if new_name.strip().lower() in ("clear", "reset", "remove", "none"):
        new_name = None
    elif len(new_name) > 32:
        await ctx.send("Nicknames max out at 32 characters.")
        return
    try:
        await member.edit(nick=new_name, reason=f"Renamed by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send(
            "I can't rename them. I need **Manage Nicknames**, and my role must sit "
            "above theirs (I can never rename the server owner)."
        )
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that rename: {e}")
        return
    if new_name is None:
        await ctx.send(f"Cleared {member.display_name}'s nickname.")
    else:
        await ctx.send(f"Renamed {member.mention} to **{new_name}**.")


@bot.hybrid_command(name="ainick", description="AI-styled rename (owner only)")
@check_privileged()
@commands.cooldown(rate=1, per=10.0, type=commands.BucketType.user)
async def ainick_cmd(ctx: commands.Context, member: discord.Member, *, vibe: str):
    """AI invents a nickname, then applies it. Usage: !ainick @user spooky vampire style"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    messages = [
        {"role": "system", "content": (
            "You invent Discord nicknames. Reply with ONLY the nickname, no quotes, "
            "no explanation, max 32 characters."
        )},
        {"role": "user", "content": (
            f"Current name: {member.display_name}. Style wanted: {vibe}."
        )},
    ]
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            status, payload = await post_chat(
                session, messages, anon_id(ctx.author.id),
                max_tokens=30, temperature=0.9,
            )
    if status != 200:
        await ctx.send(error_message(status, payload))
        return
    name = (extract_reply(payload) or "").strip().strip("'\"")[:32]
    if not name:
        await ctx.send("The AI came up empty. Try a different vibe.")
        return
    try:
        await member.edit(nick=name, reason=f"AI rename by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send(
            f"I came up with **{name}** but can't apply it - check my **Manage Nicknames** "
            "permission and role position."
        )
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that rename: {e}")
        return
    await ctx.send(f"Renamed {member.mention} to **{name}**.")


@bot.hybrid_command(name="timeout", description="Timeout a member (owner only)")
@check_privileged()
async def timeout_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason"):
    """Timeout someone. Usage: !timeout @user 10m spamming"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    dur = parse_duration(duration)
    if dur is None or dur.total_seconds() < 5:
        await ctx.send("Usage: `!timeout @user 10m [reason]` (try `10s`, `30m`, `2h`, `1d`, max 28d).")
        return
    if dur > timedelta(days=28):
        dur = timedelta(days=28)
    try:
        await member.timeout(dur, reason=f"{reason} (by {ctx.author} via Celestial)")
    except discord.Forbidden:
        await ctx.send("I can't timeout them. I need **Moderate Members** and a higher role.")
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that timeout: {e}")
        return
    await ctx.send(f"⏳ {member.mention} timed out for **{duration}**. Reason: {reason}")


@bot.hybrid_command(name="untimeout", description="Remove a timeout (owner only)")
@check_privileged()
async def untimeout_cmd(ctx: commands.Context, member: discord.Member):
    """Remove someone's timeout. Usage: !untimeout @user"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    try:
        await member.timeout(None, reason=f"Untimeout by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send("I can't untimeout them (need **Moderate Members**).")
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that: {e}")
        return
    await ctx.send(f"✅ {member.mention} is free to chat again.")


@bot.hybrid_command(name="slowmode", description="Set channel slowmode (owner only)")
@check_privileged()
async def slowmode_cmd(ctx: commands.Context, delay: str):
    """Set slowmode. Usage: !slowmode 10 | !slowmode 1m | !slowmode off"""
    if ctx.guild is None or not hasattr(ctx.channel, "edit"):
        await ctx.send("Use this in a server text channel.")
        return
    if delay.strip().lower() in ("off", "0", "0s", "none"):
        seconds = 0
    else:
        dur = parse_duration(delay)
        if dur is None:
            try:
                seconds = int(delay)
            except ValueError:
                await ctx.send("Usage: `!slowmode 10` (seconds), `!slowmode 1m`, or `!slowmode off`.")
                return
        else:
            seconds = int(dur.total_seconds())
    seconds = max(0, min(21600, seconds))
    try:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send("I can't edit this channel (need **Manage Channels**).")
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that: {e}")
        return
    await ctx.send("🐢 Slowmode off." if seconds == 0 else f"🐢 Slowmode set to **{seconds}s**.")


@bot.hybrid_command(name="lock", description="Lock this channel (owner only)")
@check_privileged()
async def lock_cmd(ctx: commands.Context):
    """Lock this channel (@everyone can't send). Usage: !lock"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False,
                                          reason=f"Locked by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send("I can't lock this (need **Manage Channels** / **Manage Roles**).")
        return
    await ctx.send("🔒 Channel locked.")


@bot.hybrid_command(name="unlock", description="Unlock this channel (owner only)")
@check_privileged()
async def unlock_cmd(ctx: commands.Context):
    """Unlock this channel. Usage: !unlock"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None,
                                          reason=f"Unlocked by {ctx.author} via Celestial")
    except discord.Forbidden:
        await ctx.send("I can't unlock this (need **Manage Channels** / **Manage Roles**).")
        return
    await ctx.send("🔓 Channel unlocked.")


@bot.hybrid_command(name="announce", description="Post an announcement (owner only)")
@check_privileged()
async def announce_cmd(ctx: commands.Context, channel: discord.TextChannel, *, text: str):
    """Post an embed announcement. Usage: !announce #news Title | body text"""
    parts = [p.strip() for p in text.split("|", 1)]
    if len(parts) < 2:
        await ctx.send("Usage: `!announce #channel Title | body text`")
        return
    embed = discord.Embed(title=f"📢 {parts[0][:256]}", description=parts[1][:4096], color=0xFEE75C)
    embed.set_footer(text=f"Announced by {ctx.author.display_name}")
    try:
        await channel.send(embed=_pretty(embed))
    except discord.Forbidden:
        await ctx.send("I can't send there.")
        return
    await ctx.send(f"Posted in {channel.mention}. ✅")


@bot.hybrid_command(name="clear", description="Delete recent messages (owner only)")
@check_privileged()
async def clear_cmd(ctx: commands.Context, amount: int = 5):
    """Delete recent messages. Usage: !clear 10 (max 30)"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    amount = max(1, min(30, amount))
    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
    except discord.Forbidden:
        await ctx.send("I can't delete here (need **Manage Messages** + **Read Message History**).")
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that: {e}")
        return
    await ctx.send(f"🧹 Cleared {max(0, len(deleted) - 1)} messages.")


@bot.hybrid_command(name="aishout", description="AI-polished @everyone post (owner only)")
@check_privileged()
@commands.cooldown(rate=1, per=60.0, type=commands.BucketType.channel)
async def aishout_cmd(ctx: commands.Context, *, draft: str):
    """AI-polish a draft and post it with @everyone. Usage: !aishout [#channel] game night friday 8pm"""
    if ctx.guild is None:
        await ctx.send("Use this in a server.")
        return
    target = ctx.channel
    text = draft.strip()
    ch_mentions = ctx.message.channel_mentions if ctx.message else []
    if ch_mentions:
        target = ch_mentions[0]
        text = text.replace(target.mention, "").strip()
    if len(text) < 3:
        await ctx.send("Usage: `!aishout [#channel] <your announcement draft>`")
        return
    if need_key(ctx):
        await no_key_msg(ctx)
        return
    messages = [
        {"role": "system", "content": (
            "You polish Discord server announcements. Rewrite the draft to be clear, "
            "energetic and short (under 1200 characters). Keep every fact, date and time "
            "exactly as given. No markdown headers. Same language as the draft. "
            "Reply ONLY with the announcement text. Live context: " + server_context(ctx)
        )},
        {"role": "user", "content": text},
    ]
    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            status, payload = await post_chat(session, messages, anon_id(ctx.author.id),
                                              max_tokens=500, temperature=0.7)
    if status != 200:
        await ctx.send(error_message(status, payload))
        return
    polished = (extract_reply(payload) or "").strip() or text
    try:
        await target.send("@everyone\n\n" + polished[:1900],
                          allowed_mentions=discord.AllowedMentions(everyone=True))
    except discord.Forbidden:
        await ctx.send(f"I can't post in {target.mention} (need Send Messages + Mention Everyone there).")
        return
    except discord.HTTPException as e:
        await ctx.send(f"Discord refused that post: {e}")
        return
    if target.id != ctx.channel.id:
        await ctx.send(f"📢 Posted in {target.mention}.")


@bot.hybrid_command(name="remind", description="Set yourself a reminder (owner only)")
@check_privileged()
async def remind_cmd(ctx: commands.Context, duration: str, *, text: str):
    """Set a reminder. Usage: !remind 10m check the oven"""
    dur = parse_duration(duration)
    if dur is None:
        await ctx.send("Usage: `!remind 10m <what>` (try `30s`, `10m`, `2h`; max 24h).")
        return
    secs = dur.total_seconds()
    if secs < 10 or secs > 86400:
        await ctx.send("Between 10 seconds and 24 hours please. (Restarts clear pending reminders.)")
        return
    asyncio.create_task(_schedule_reminder(ctx.author.id, ctx.channel, secs, text.strip()))
    await ctx.send(f"⏰ Got it — I'll remind you in **{duration}**: {text.strip()[:200]}")


@bot.hybrid_command(name="setavatar", description="Change Celestial's profile picture (owner only)")
@check_privileged()
async def setavatar_cmd(ctx: commands.Context, image: discord.Attachment = None):
    """Change profile picture. Usage: !setavatar (with an attached image)"""
    att = image
    if att is None and ctx.message:
        atts = ctx.message.attachments
        att = atts[0] if atts else None
    if att is None:
        await ctx.send("Attach an image with the command: `!setavatar` + image.")
        return
    if not (att.content_type or "").startswith("image/"):
        await ctx.send("That file isn't an image.")
        return
    if att.size > 8 * 1024 * 1024:
        await ctx.send("Image too big (max 8 MB).")
        return
    try:
        data = await att.read()
        await bot.user.edit(avatar=data)
    except discord.HTTPException:
        await ctx.send("Discord refused it (avatars can only change twice per hour - try later).")
        return
    await ctx.send("✅ Profile picture updated!")


@bot.hybrid_command(name="model", description="Show or change the AI model")
async def model(ctx: commands.Context, *, new_model: str = None):
    """Show or change the AI model (until restart)."""
    global current_model
    if new_model is None:
        await ctx.send(f"Current model: `{current_model}`")
        return
    current_model = new_model
    await ctx.send(f"Model set to `{current_model}` (resets on restart).")


@model.error
async def model_error(ctx: commands.Context, error: commands.CommandError):
    await ctx.send(f"Could not change model: {error}")


@ai.error
async def ai_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!ai <your question>`")


@chat.error
async def chat_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!chat <your message>`")


@aiembed.error
async def aiembed_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!aiembed <describe the embed>`")


@summarize.error
async def summarize_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Usage: `!summarize [5-30]`")


@aishout_cmd.error
async def aishout_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Usage: `!aishout [#channel] <your announcement draft>`")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    # Per-command handlers already replied - avoid double messages.
    try:
        if ctx.command is not None and ctx.command.has_error_handler():
            return
    except Exception:
        pass
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🔒 Only my owner can use that.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down - try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing input. Try `!help {ctx.command}`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Bad argument: {error}")
    else:
        await ctx.send(f"Something went wrong: {error}")


@bot.tree.error
async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down - try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "🔒 Only my owner can use that."
    else:
        msg = f"Something went wrong: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


bot.run(TOKEN, log_handler=None)
