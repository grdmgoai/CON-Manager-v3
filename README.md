# CON Manager v3

A Discord bot for organizing Conflict of Nations community matches. All bot commands, messages, and code identifiers are in English.

## Features

- Configurable public match panels: mode, speed, player count, map, description, and rules.
- Queue buttons with a configurable player cap; queue locks when full.
- Private team channel and automatic team-leader assignment.
- `/search` starts a manual search; the organizer enters the game code with `/set_match_code`.
- Public announcement and direct-message attempts for the match code.
- `/confirm_entry` and `/my_country` for each player.
- Timer starts only after all configured players confirm entry and country.
- Pause/resume timer, status, manual logs, CSV export, and match history.
- SQLite persistence and persistent queue buttons after restart.
- Admin command to configure public announcement channel.

## Important integration limitation

This release does **not** connect to an official Conflict of Nations API and does not automatically read game events. `/search` is a Discord workflow, not an in-game search. Game-day display is an elapsed-time estimate only. Manual event logging remains available while a separate read-only community integration is investigated and validated.

## Windows setup (Python 3.12)

1. Install Python 3.12 from https://www.python.org/downloads/windows/ and enable **Add Python to PATH**.
2. Extract this ZIP to a normal folder, for example `C:\CON_Manager_v3`.
3. In File Explorer, open that folder. Click the address bar, type `cmd`, and press Enter.
4. Create a virtual environment and install dependencies:

   ```bat
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

5. Create `.env` by copying `.env.example` and rename the copy to `.env`. Put your new bot token after `DISCORD_TOKEN=`. Set `GUILD_ID=` to your test server's numeric ID. Do not share `.env`.
6. In the Discord Developer Portal, enable the bot and invite it to the server with View Channels, Send Messages, Embed Links, Read Message History, Manage Channels, and Send Messages in Threads if needed. Slash commands require the `applications.commands` scope.
7. Run:

   ```bat
   .venv\Scripts\activate
   python bot.py
   ```

8. Keep the terminal open while the bot is running. Stop it with Ctrl+C.

## First use

1. Run `/configure_announcements` and select the public announcements channel.
2. Run `/panel` and fill in all required fields: name, game mode, game speed, player count, and map name. For custom speed, fill `custom_speed` too.
3. Players click **Join Queue**. When the configured number is reached, the bot locks the queue and creates a private team channel.
4. A team member with access runs `/search match_id:<ID>`.
5. The organizer enters the code with `/set_match_code match_id:<ID> code:<GAME_CODE>`.
6. Each player runs `/confirm_entry match_id:<ID>` and `/my_country match_id:<ID> country:<COUNTRY>`.
7. Once all players have confirmed both items, the timer starts automatically.
8. Use `/status`, `/register_event`, `/logs`, `/pause_timer`, `/resume_timer`, `/export_report`, and `/end_match` as needed.

The numeric Match ID is shown in the panel footer. Only the organizer, leader, participants, or server administrators can manage/view match records.

## Security

- Never share your `.env` file or bot token.
- Do not add game-account passwords or cookies to this bot.
- The game monitor is not enabled in this version. Do not assume automatic game event detection is available.
- Back up `con_manager.db` periodically while the bot is stopped.
