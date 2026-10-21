import asyncio
import csv
import io
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "con_manager.db"
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else None

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

GAME_MODES = [
    app_commands.Choice(name="4x", value="4x"),
    app_commands.Choice(name="Overkill", value="Overkill"),
    app_commands.Choice(name="Standard", value="Standard"),
    app_commands.Choice(name="Custom", value="Custom"),
]
GAME_SPEEDS = [
    app_commands.Choice(name="1x", value="1x"),
    app_commands.Choice(name="4x", value="4x"),
    app_commands.Choice(name="Custom", value="Custom"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            announcement_channel_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            organizer_id INTEGER NOT NULL,
            leader_id INTEGER,
            panel_channel_id INTEGER,
            panel_message_id INTEGER,
            team_channel_id INTEGER,
            announcement_channel_id INTEGER,
            name TEXT NOT NULL,
            game_mode TEXT NOT NULL,
            game_speed TEXT NOT NULL,
            custom_speed TEXT,
            player_count INTEGER NOT NULL,
            map_name TEXT NOT NULL,
            description TEXT,
            rules TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            searching INTEGER NOT NULL DEFAULT 0,
            match_code TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            paused_at TEXT,
            paused_seconds INTEGER NOT NULL DEFAULT 0,
            ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS players (
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            invited INTEGER NOT NULL DEFAULT 0,
            entry_confirmed INTEGER NOT NULL DEFAULT 0,
            country TEXT,
            country_confirmed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(match_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            author_id INTEGER,
            event_type TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


def fetch_match(match_id: int):
    with db() as con:
        return con.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()


def event(match_id: int, author_id: int | None, event_type: str, details: str) -> None:
    with db() as con:
        con.execute("INSERT INTO events(match_id, author_id, event_type, details, created_at) VALUES(?,?,?,?,?)",
                    (match_id, author_id, event_type, details[:1800], now_iso()))


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild


def has_match_access(match, member: discord.Member) -> bool:
    if is_admin(member) or member.id == match["organizer_id"] or member.id == match["leader_id"]:
        return True
    with db() as con:
        return con.execute("SELECT 1 FROM players WHERE match_id=? AND user_id=?", (match["id"], member.id)).fetchone() is not None


def get_players(match_id: int):
    with db() as con:
        return con.execute("SELECT * FROM players WHERE match_id=? ORDER BY joined_at", (match_id,)).fetchall()


def match_embed(match_id: int) -> discord.Embed:
    match = fetch_match(match_id)
    players = get_players(match_id)
    names = [f"<@{p['user_id']}>" for p in players]
    slots = "\n".join(f"• {name}" for name in names) or "No players yet"
    embed = discord.Embed(title=f"🎮 Match Lobby: {match['name']}", color=discord.Color.blurple())
    embed.add_field(name="Game Mode", value=match["game_mode"], inline=True)
    speed = match["custom_speed"] if match["game_speed"] == "Custom" and match["custom_speed"] else match["game_speed"]
    embed.add_field(name="Game Speed", value=speed, inline=True)
    embed.add_field(name="Player Count", value=f"{len(players)}/{match['player_count']}", inline=True)
    embed.add_field(name="Map", value=match["map_name"] or "Not specified", inline=True)
    embed.add_field(name="Status", value=match["status"].replace("_", " ").title(), inline=True)
    if match["description"]:
        embed.add_field(name="Description", value=match["description"][:1024], inline=False)
    if match["rules"]:
        embed.add_field(name="Rules", value=match["rules"][:1024], inline=False)
    embed.add_field(name=f"Players ({len(players)}/{match['player_count']})", value=slots[:1024], inline=False)
    embed.set_footer(text=f"Match ID: {match_id} • Join the queue or leave at any time before it locks")
    return embed


async def refresh_panel(match_id: int, client: discord.Client | None = None) -> None:
    match = fetch_match(match_id)
    if not match or not match["panel_channel_id"] or not match["panel_message_id"]:
        return
    client = client or bot
    channel = client.get_channel(match["panel_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(match["panel_message_id"])
        view = MatchPanelView(match_id, disabled=match["status"] != "open")
        await message.edit(embed=match_embed(match_id), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def create_team_channel(guild: discord.Guild, match_id: int) -> discord.TextChannel | None:
    match = fetch_match(match_id)
    players = get_players(match_id)
    if not match or len(players) != match["player_count"]:
        return None
    if match["team_channel_id"]:
        channel = guild.get_channel(match["team_channel_id"])
        return channel if isinstance(channel, discord.TextChannel) else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    for p in players:
        member = guild.get_member(p["user_id"])
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    if match["organizer_id"] not in [p["user_id"] for p in players]:
        organizer = guild.get_member(match["organizer_id"])
        if organizer:
            overwrites[organizer] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", match["name"].lower()).strip("-")[:35] or "match"
    try:
        channel = await guild.create_text_channel(
            name=f"match-{match_id}-{slug}", overwrites=overwrites,
            topic=f"Private CON Manager team channel • Match ID {match_id} • Do not share match credentials here."
        )
    except discord.Forbidden:
        return None
    with db() as con:
        con.execute("UPDATE matches SET team_channel_id=?, leader_id=COALESCE(leader_id, ?) WHERE id=?",
                    (channel.id, players[0]["user_id"], match_id))
    mentions = " ".join(f"<@{p['user_id']}>" for p in players)
    await channel.send(
        f"## Match Team — {match['name']}\n{mentions}\n\n"
        f"**Mode:** {match['game_mode']} · **Speed:** {match['custom_speed'] if match['game_speed']=='Custom' and match['custom_speed'] else match['game_speed']}\n"
        f"**Players:** {match['player_count']} · **Map:** {match['map_name']}\n\n"
        "The organizer can use `/search` to begin searching, then `/set_match_code` to share the match code. "
        "Every player must confirm entry and country before the timer starts."
    )
    event(match_id, match["organizer_id"], "team_created", f"Private team channel created: #{channel.name}")
    return channel


class MatchPanelView(discord.ui.View):
    def __init__(self, match_id: int, disabled: bool = False):
        super().__init__(timeout=None)
        self.match_id = match_id
        join = discord.ui.Button(label="Join Queue", style=discord.ButtonStyle.success, emoji="➕", custom_id=f"con:join:{match_id}", disabled=disabled)
        leave = discord.ui.Button(label="Leave Queue", style=discord.ButtonStyle.secondary, emoji="➖", custom_id=f"con:leave:{match_id}", disabled=disabled)
        join.callback = self.join_callback
        leave.callback = self.leave_callback
        self.add_item(join)
        self.add_item(leave)

    async def join_callback(self, interaction: discord.Interaction):
        match = fetch_match(self.match_id)
        if not match or match["status"] != "open":
            await interaction.response.send_message("This queue is no longer open.", ephemeral=True)
            return
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This action is only available inside the server.", ephemeral=True)
            return
        with db() as con:
            existing = con.execute("SELECT 1 FROM players WHERE match_id=? AND user_id=?", (self.match_id, interaction.user.id)).fetchone()
            count = con.execute("SELECT COUNT(*) AS n FROM players WHERE match_id=?", (self.match_id,)).fetchone()["n"]
            if existing:
                await interaction.response.send_message("You are already in this queue.", ephemeral=True)
                return
            if count >= match["player_count"]:
                await interaction.response.send_message("All player slots are filled.", ephemeral=True)
                return
            con.execute("INSERT INTO players(match_id,user_id,joined_at) VALUES(?,?,?)", (self.match_id, interaction.user.id, now_iso()))
        event(self.match_id, interaction.user.id, "player_joined", f"{interaction.user} joined the queue")
        await interaction.response.edit_message(embed=match_embed(self.match_id), view=MatchPanelView(self.match_id))
        count_now = len(get_players(self.match_id))
        if count_now >= match["player_count"]:
            with db() as con:
                con.execute("UPDATE matches SET status='confirming', leader_id=COALESCE(leader_id, ?) WHERE id=?", (get_players(self.match_id)[0]["user_id"], self.match_id))
            channel = await create_team_channel(interaction.guild, self.match_id)
            await refresh_panel(self.match_id)
            if channel:
                await interaction.followup.send(f"Queue complete. The private team channel is {channel.mention}.", ephemeral=True)
            else:
                await interaction.followup.send("Queue complete, but I could not create the private team channel. Check the bot's Manage Channels permission.", ephemeral=True)
        else:
            await interaction.followup.send(f"You joined the queue ({count_now}/{match['player_count']}).", ephemeral=True)

    async def leave_callback(self, interaction: discord.Interaction):
        match = fetch_match(self.match_id)
        if not match or match["status"] != "open":
            await interaction.response.send_message("The queue is locked; ask an administrator for help.", ephemeral=True)
            return
        with db() as con:
            cur = con.execute("DELETE FROM players WHERE match_id=? AND user_id=?", (self.match_id, interaction.user.id))
        if cur.rowcount == 0:
            await interaction.response.send_message("You are not in this queue.", ephemeral=True)
            return
        event(self.match_id, interaction.user.id, "player_left", f"User ID {interaction.user.id} left the queue")
        await interaction.response.edit_message(embed=match_embed(self.match_id), view=self)


async def resolve_match_from_interaction(interaction: discord.Interaction, match_id: int):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True)
        return None
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command must be used in the server.", ephemeral=True)
        return None
    if not has_match_access(match, interaction.user):
        await interaction.response.send_message("You are not authorized to manage this match.", ephemeral=True)
        return None
    return match


@bot.event
async def on_ready():
    init_db()
    with db() as con:
        rows = con.execute("SELECT id, panel_message_id FROM matches WHERE status='open' AND panel_message_id IS NOT NULL").fetchall()
    for row in rows:
        bot.add_view(MatchPanelView(row["id"]), message_id=row["panel_message_id"])
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"Connected as {bot.user}. Synced {len(synced)} commands to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"Connected as {bot.user}. Synced {len(synced)} global commands.")
    except Exception as exc:
        print(f"Command sync failed: {exc}")


@bot.tree.command(name="panel", description="Create a configurable public match queue panel.")
@app_commands.describe(name="Match or team name", game_mode="Game mode", game_speed="Game speed", player_count="Number of players", map_name="Map name or type", description="Optional match description", rules="Optional match rules", custom_speed="Speed label when Game Speed is Custom")
@app_commands.choices(game_mode=GAME_MODES, game_speed=GAME_SPEEDS)
@app_commands.checks.has_permissions(manage_guild=True)
async def panel(interaction: discord.Interaction, name: app_commands.Range[str, 1, 80], game_mode: app_commands.Choice[str], game_speed: app_commands.Choice[str], player_count: app_commands.Range[int, 2, 100], map_name: app_commands.Range[str, 1, 80], description: str = "", rules: str = "", custom_speed: str = ""):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this command in a server text channel.", ephemeral=True)
        return
    with db() as con:
        cur = con.execute("INSERT INTO matches(guild_id,organizer_id,leader_id,panel_channel_id,announcement_channel_id,name,game_mode,game_speed,custom_speed,player_count,map_name,description,rules,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                          (interaction.guild.id, interaction.user.id, interaction.user.id, interaction.channel.id, interaction.channel.id, name, game_mode.value, game_speed.value, custom_speed[:40], player_count, map_name, description[:1000], rules[:1000], now_iso()))
        match_id = cur.lastrowid
    view = MatchPanelView(match_id)
    await interaction.response.send_message(embed=match_embed(match_id), view=view)
    message = await interaction.original_response()
    with db() as con:
        con.execute("UPDATE matches SET panel_message_id=? WHERE id=?", (message.id, match_id))
    bot.add_view(view, message_id=message.id)
    event(match_id, interaction.user.id, "panel_created", "Public match panel created")


@bot.tree.command(name="search", description="Start searching for a match; the organizer will enter the code manually.")
@app_commands.describe(match_id="CON Manager match ID")
async def search(interaction: discord.Interaction, match_id: int):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match:
        return
    if match["status"] not in ("confirming", "searching"):
        await interaction.response.send_message("This match must have a full queue before searching starts.", ephemeral=True)
        return
    with db() as con:
        con.execute("UPDATE matches SET searching=1,status='searching' WHERE id=?", (match_id,))
    event(match_id, interaction.user.id, "search_started", "Organizer started manual match search")
    channel_id = match["announcement_channel_id"] or match["panel_channel_id"]
    channel = interaction.client.get_channel(channel_id)
    message = f"🔎 **Match Search Started** — **{match['name']}** (Match ID: `{match_id}`)\nThe organizer is looking for a game. The match code will be shared here and sent privately when available."
    if channel:
        await channel.send(message)
    await interaction.response.send_message("Match search started and announcement posted.", ephemeral=True)


@bot.tree.command(name="set_match_code", description="Set the match code and notify the public channel and all players.")
@app_commands.describe(match_id="CON Manager match ID", code="Match code or game ID")
async def set_match_code(interaction: discord.Interaction, match_id: int, code: app_commands.Range[str, 1, 100]):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match:
        return
    if match["status"] not in ("searching", "confirming"):
        await interaction.response.send_message("The match is not ready to receive a code.", ephemeral=True)
        return
    with db() as con:
        con.execute("UPDATE matches SET match_code=?, status='confirming' WHERE id=?", (code, match_id))
    event(match_id, interaction.user.id, "match_code_set", f"Match code set to {code}")
    public_channel = interaction.client.get_channel(match["announcement_channel_id"] or match["panel_channel_id"])
    text = f"🎮 **Match Code Available — {match['name']}**\n**Code:** `{code}`\nAll players: join the game, then use `/confirm_entry` and `/my_country` in the private team channel. The timer starts after everyone confirms both."
    if public_channel:
        await public_channel.send(text)
    failed_dms = 0
    for player in get_players(match_id):
        try:
            user = await interaction.client.fetch_user(player["user_id"])
            await user.send(text)
            with db() as con:
                con.execute("UPDATE players SET invited=1 WHERE match_id=? AND user_id=?", (match_id, player["user_id"]))
        except (discord.Forbidden, discord.HTTPException):
            failed_dms += 1
    await interaction.response.send_message(f"Match code posted and DMs attempted. Failed DMs: {failed_dms}. Players can still read the code in the announcement channel.", ephemeral=True)


@bot.tree.command(name="confirm_entry", description="Confirm that you successfully joined the game.")
@app_commands.describe(match_id="CON Manager match ID")
async def confirm_entry(interaction: discord.Interaction, match_id: int):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True); return
    with db() as con:
        player = con.execute("SELECT * FROM players WHERE match_id=? AND user_id=?", (match_id, interaction.user.id)).fetchone()
        if not player:
            await interaction.response.send_message("You are not registered for this match.", ephemeral=True); return
        if not match["match_code"]:
            await interaction.response.send_message("The organizer has not entered the match code yet.", ephemeral=True); return
        con.execute("UPDATE players SET entry_confirmed=1 WHERE match_id=? AND user_id=?", (match_id, interaction.user.id))
    event(match_id, interaction.user.id, "entry_confirmed", f"User ID {interaction.user.id} confirmed game entry")
    await maybe_start_timer(match_id, interaction.client)
    await interaction.response.send_message("Entry confirmed. Make sure you also set your country with `/my_country`.", ephemeral=True)


@bot.tree.command(name="my_country", description="Set or update your country for this match.")
@app_commands.describe(match_id="CON Manager match ID", country="Country name as shown in the game")
async def my_country(interaction: discord.Interaction, match_id: int, country: app_commands.Range[str, 1, 80]):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True); return
    with db() as con:
        player = con.execute("SELECT * FROM players WHERE match_id=? AND user_id=?", (match_id, interaction.user.id)).fetchone()
        if not player:
            await interaction.response.send_message("You are not registered for this match.", ephemeral=True); return
        con.execute("UPDATE players SET country=?, country_confirmed=1 WHERE match_id=? AND user_id=?", (country, match_id, interaction.user.id))
    event(match_id, interaction.user.id, "country_set", f"Country assigned: {country}")
    await maybe_start_timer(match_id, interaction.client)
    await interaction.response.send_message(f"Country saved: **{country}**. Your confirmation has been recorded.", ephemeral=True)


async def maybe_start_timer(match_id: int, client: discord.Client):
    match = fetch_match(match_id)
    players = get_players(match_id)
    if not match or match["started_at"] or len(players) != match["player_count"] or not players:
        return
    if not all(p["entry_confirmed"] and p["country_confirmed"] and p["country"] for p in players):
        return
    started = now_iso()
    with db() as con:
        con.execute("UPDATE matches SET started_at=?, status='active' WHERE id=? AND started_at IS NULL", (started, match_id))
    event(match_id, None, "timer_started", f"Timer started at {started}; all players confirmed entry and country")
    match = fetch_match(match_id)
    channel = client.get_channel(match["team_channel_id"]) if match["team_channel_id"] else None
    notice = f"✅ **Match Started!** All {match['player_count']} players confirmed entry and country.\nStart time (UTC): `{started}`\nUse `/status match_id:{match_id}` to check elapsed time."
    if channel:
        await channel.send(notice)
    public = client.get_channel(match["announcement_channel_id"] or match["panel_channel_id"])
    if public:
        await public.send(f"✅ **{match['name']} has officially started.** All players confirmed. Match ID: `{match_id}`.")


@bot.tree.command(name="status", description="Show match status, confirmations, and elapsed time.")
@app_commands.describe(match_id="CON Manager match ID")
async def status(interaction: discord.Interaction, match_id: int):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True); return
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not has_match_access(match, interaction.user):
        await interaction.response.send_message("You are not authorized to view this match status.", ephemeral=True); return
    players = get_players(match_id)
    embed = discord.Embed(title=f"Match Status — {match['name']}", color=discord.Color.blue())
    embed.add_field(name="Match", value=f"`{match_id}` · {match['status'].title()}", inline=False)
    embed.add_field(name="Mode / Speed", value=f"{match['game_mode']} / {match['custom_speed'] if match['game_speed']=='Custom' and match['custom_speed'] else match['game_speed']}", inline=True)
    embed.add_field(name="Players", value=f"{len(players)}/{match['player_count']}", inline=True)
    if match["match_code"]:
        embed.add_field(name="Match Code", value=f"`{match['match_code']}`", inline=False)
    lines = []
    for p in players:
        entry = "Confirmed" if p["entry_confirmed"] else "Pending"
        country = p["country"] or "Not selected"
        country_state = "Confirmed" if p["country_confirmed"] else "Pending"
        lines.append(f"<@{p['user_id']}> — Entry: **{entry}** · Country: **{country}** ({country_state})")
    embed.add_field(name="Player Confirmations", value="\n".join(lines)[:1024] or "No players yet", inline=False)
    if match["started_at"]:
        start = datetime.fromisoformat(match["started_at"])
        end = datetime.fromisoformat(match["ended_at"]) if match["ended_at"] else datetime.now(timezone.utc)
        paused_seconds = match["paused_seconds"]
        if match["paused_at"] and not match["ended_at"]:
            paused_seconds += max(0, int((end - datetime.fromisoformat(match["paused_at"])).total_seconds()))
        elapsed = max(0, int((end - start).total_seconds()) - paused_seconds)
        days = elapsed // 86400 + 1
        embed.add_field(name="Elapsed Time", value=f"{elapsed // 86400}d {(elapsed % 86400) // 3600}h {(elapsed % 3600) // 60}m", inline=True)
        embed.add_field(name="Estimated Game Day", value=f"Day {days} (elapsed-time estimate; not synchronized to the game server)", inline=False)
    if match["started_at"]: embed.add_field(name="Started At (UTC)", value=match["started_at"], inline=False)
    if match["ended_at"]: embed.add_field(name="Ended At (UTC)", value=match["ended_at"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="pause_timer", description="Pause the match elapsed-time counter.")
@app_commands.describe(match_id="CON Manager match ID")
async def pause_timer(interaction: discord.Interaction, match_id: int):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match: return
    if not match["started_at"] or match["ended_at"] or match["paused_at"]:
        await interaction.response.send_message("The timer cannot be paused in its current state.", ephemeral=True); return
    with db() as con: con.execute("UPDATE matches SET paused_at=? WHERE id=?", (now_iso(), match_id))
    event(match_id, interaction.user.id, "timer_paused", "Match timer paused")
    await interaction.response.send_message("Match timer paused.", ephemeral=True)


@bot.tree.command(name="resume_timer", description="Resume a paused match timer.")
@app_commands.describe(match_id="CON Manager match ID")
async def resume_timer(interaction: discord.Interaction, match_id: int):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match: return
    if not match["paused_at"] or match["ended_at"]:
        await interaction.response.send_message("The timer is not paused.", ephemeral=True); return
    paused = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(match["paused_at"])).total_seconds()))
    with db() as con: con.execute("UPDATE matches SET paused_at=NULL, paused_seconds=paused_seconds+? WHERE id=?", (paused, match_id))
    event(match_id, interaction.user.id, "timer_resumed", f"Match timer resumed after {paused} seconds")
    await interaction.response.send_message("Match timer resumed.", ephemeral=True)


@bot.tree.command(name="register_event", description="Add a manual event to the match log.")
@app_commands.describe(match_id="CON Manager match ID", event_type="Short event category", details="Event details")
async def register_event(interaction: discord.Interaction, match_id: int, event_type: app_commands.Range[str, 1, 50], details: app_commands.Range[str, 1, 1500]):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match: return
    event(match_id, interaction.user.id, event_type, details)
    await interaction.response.send_message("Event added to the match log.", ephemeral=True)


@bot.tree.command(name="logs", description="View recent events recorded for a match.")
@app_commands.describe(match_id="CON Manager match ID", limit="Number of recent events to show")
async def logs(interaction: discord.Interaction, match_id: int, limit: app_commands.Range[int, 1, 20] = 10):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True); return
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not has_match_access(match, interaction.user):
        await interaction.response.send_message("You are not authorized to view these logs.", ephemeral=True); return
    with db() as con:
        rows = con.execute("SELECT * FROM events WHERE match_id=? ORDER BY id DESC LIMIT ?", (match_id, limit)).fetchall()
    embed = discord.Embed(title=f"Match Logs — {match['name']}", color=discord.Color.dark_teal())
    if rows:
        embed.description = "\n".join(f"`{r['created_at']}` · **{r['event_type']}** · {r['details']} · by {('<@'+str(r['author_id'])+'>') if r['author_id'] else 'System'}" for r in rows)[:4000]
    else:
        embed.description = "No events recorded yet."
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="end_match", description="End a match and preserve its history.")
@app_commands.describe(match_id="CON Manager match ID")
async def end_match(interaction: discord.Interaction, match_id: int):
    match = await resolve_match_from_interaction(interaction, match_id)
    if not match: return
    if match["ended_at"]:
        await interaction.response.send_message("This match has already ended.", ephemeral=True); return
    ended = now_iso()
    with db() as con: con.execute("UPDATE matches SET ended_at=?, status='ended', paused_at=NULL WHERE id=?", (ended, match_id))
    event(match_id, interaction.user.id, "match_ended", f"Match ended at {ended}")
    await refresh_panel(match_id)
    await interaction.response.send_message(f"Match ended. History preserved. Use `/export_report match_id:{match_id}` to download a report.", ephemeral=True)


@bot.tree.command(name="export_report", description="Export match details, players, and logs as a CSV file.")
@app_commands.describe(match_id="CON Manager match ID")
async def export_report(interaction: discord.Interaction, match_id: int):
    match = fetch_match(match_id)
    if not match:
        await interaction.response.send_message("Match ID not found.", ephemeral=True); return
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not has_match_access(match, interaction.user):
        await interaction.response.send_message("You are not authorized to export this match.", ephemeral=True); return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["record_type", "timestamp", "author_id", "event_type", "details"])
    writer.writerow(["match", match["created_at"], match["organizer_id"], "match_summary", f"id={match_id};name={match['name']};mode={match['game_mode']};speed={match['game_speed']};players={match['player_count']};map={match['map_name']};code={match['match_code'] or ''};started={match['started_at'] or ''};ended={match['ended_at'] or ''}"])
    for p in get_players(match_id):
        writer.writerow(["player", p["joined_at"], p["user_id"], "player_record", f"country={p['country'] or ''};entry_confirmed={p['entry_confirmed']};country_confirmed={p['country_confirmed']}"])
    with db() as con: rows = con.execute("SELECT * FROM events WHERE match_id=? ORDER BY id", (match_id,)).fetchall()
    for r in rows: writer.writerow(["event", r["created_at"], r["author_id"] or "", r["event_type"], r["details"]])
    data = output.getvalue().encode("utf-8-sig")
    file = discord.File(io.BytesIO(data), filename=f"con_match_{match_id}_report.csv")
    await interaction.response.send_message("Here is your match report.", file=file, ephemeral=True)


@bot.tree.command(name="configure_announcements", description="Set the channel used for public match announcements.")
@app_commands.describe(channel="Public announcement channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def configure_announcements(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True); return
    with db() as con:
        con.execute("INSERT INTO settings(guild_id,announcement_channel_id) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET announcement_channel_id=excluded.announcement_channel_id", (interaction.guild.id, channel.id))
        con.execute("UPDATE matches SET announcement_channel_id=? WHERE guild_id=? AND status IN ('open','confirming','searching','active','paused')", (channel.id, interaction.guild.id))
    await interaction.response.send_message(f"Public announcements will use {channel.mention} for new matches.", ephemeral=True)


@bot.tree.command(name="help_con", description="Show CON Manager commands and workflow.")
async def help_con(interaction: discord.Interaction):
    embed = discord.Embed(title="CON Manager — Help", color=discord.Color.blurple())
    embed.description = (
        "**Match setup:** `/panel`\n**Search:** `/search` → `/set_match_code`\n"
        "**Player confirmations:** `/confirm_entry` and `/my_country`\n**Tracking:** `/status`, `/pause_timer`, `/resume_timer`\n"
        "**Logs:** `/register_event`, `/logs`, `/export_report`\n**Finish:** `/end_match`\n"
        "**Admin:** `/configure_announcements`\n\n"
        "The official timer starts only after every configured player confirms entry and country. "
        "The displayed game day is an elapsed-time estimate, not a verified game-server day."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@panel.error
@configure_announcements.error
async def admin_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    else:
        message = "The command failed. Check the bot permissions and console output."
        print(f"Command error: {error!r}")
    if interaction.response.is_done(): await interaction.followup.send(message, ephemeral=True)
    else: await interaction.response.send_message(message, ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Create a .env file based on .env.example.")
init_db()
bot.run(TOKEN)
