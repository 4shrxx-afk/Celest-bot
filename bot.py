import hashlib
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import discord
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
    "You are a friendly Discord bot named Celestial. "
    "Answer clearly and keep replies short - a few sentences max, no markdown headers. "
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

bot = commands.Bot(command_prefix="!", intents=intents)

# Current model. Can be swapped at runtime with !model <id>
current_model = env("OPENROUTER_MODEL") or DEFAULT_MODEL


# ---------------------------------------------------------------- OpenRouter
def anon_id(user_id: int) -> str:
    """Hash of the Discord user id - the raw id is never sent anywhere."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:32]


async def ask_openrouter(session: aiohttp.ClientSession, prompt: str, user: str):
    """POST to OpenRouter. Returns (http_status, payload_dict)."""
    headers = {
        "Authorization": f"Bearer {env('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
        "X-Title": "Celestial Bot",
    }
    body = {
        "model": current_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 700,
        "temperature": 0.7,
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


# ---------------------------------------------------------------- commands
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | servers: {len(bot.guilds)} | model: {current_model}")
    print("------")


@bot.command()
async def ping(ctx: commands.Context):
    """Replies with the bot's latency."""
    await ctx.send(f"Pong! {bot.latency * 1000:.0f} ms")


@bot.command()
async def hello(ctx: commands.Context):
    """Says hello."""
    await ctx.send(f"Hello {ctx.author.display_name}!")


@bot.command()
async def echo(ctx: commands.Context, *, text: str):
    """Repeats whatever you type after !echo."""
    await ctx.send(text)


@bot.command(name="ai")
@commands.cooldown(rate=1, per=5.0, type=commands.BucketType.user)
async def ai(ctx: commands.Context, *, prompt: str):
    """Ask the AI. Usage: !ai what is hello"""
    if not env("OPENROUTER_API_KEY"):
        await ctx.send(
            "`OPENROUTER_API_KEY` is not set - add it under Render "
            "Dashboard -> Environment (get a free key at openrouter.ai/keys)."
        )
        return

    async with ctx.typing():
        async with aiohttp.ClientSession() as session:
            status, payload = await ask_openrouter(session, prompt, anon_id(ctx.author.id))

    if status != 200:
        await ctx.send(error_message(status, payload))
        return

    try:
        reply = payload["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        await ctx.send("OpenRouter returned an unexpected response.")
        return

    if not reply:
        await ctx.send("The model returned an empty reply. Try rephrasing.")
        return

    # Discord messages cap at 2000 characters.
    for chunk in (reply[i : i + 1990] for i in range(0, len(reply), 1990)):
        await ctx.send(chunk)


@bot.command(name="model")
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
    else:
        await ctx.send(f"Something went wrong: {error}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.MissingRequiredArgument)):
        return
    await ctx.send(f"Something went wrong: {error}")


bot.run(TOKEN, log_handler=None)
