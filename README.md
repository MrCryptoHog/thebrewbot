# ☕ TheBrewBot — Coffee-Themed Crypto Call Tracker

A cozy crypto-café Telegram bot. Users share contract addresses with a thesis, "brew" calls as **Gamble 🎲** or **Alpha 🏆**, then flex gains on the **Cafeboard** leaderboard.

## Features

| Feature | Description |
|---------|-------------|
| **CA Detection** | Auto-detects Ethereum (`0x…`) and Solana (base58) addresses in group messages |
| **DexScreener Integration** | Pulls live FDV, liquidity, and 24h volume for every token |
| **Call Submission** | Lock in market cap at the moment you submit — Gamble or Alpha |
| **`/pnl <CA>`** | Generates a premium Coffee Card image showing % gain, x-multiplier, thesis, and more |
| **`/cafeboard`** | Ranked leaderboard (top 15) by highest live x-multiplier per user |
| **`/refresh`** | Force-refresh the Cafeboard with live DexScreener data |
| **Daily Auto-Post** | Automatically posts an updated Cafeboard to active groups every 24 hours |

## Quick Start

### 1. Prerequisites

- Python 3.11+
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 2. Install

```bash
git clone <your-repo-url>
cd tb-telegram-bot
pip install -r requirements_brewbot.txt
```

### 3. Configure

Create a `.env` file in the project root:

```
BOT_TOKEN=your_telegram_bot_token_here
```

### 4. Run

```bash
python brewbot.py
```

The bot will create `cafebot.db` automatically on first run.

## Deployment

### Railway

1. Push your repo to GitHub.
2. Create a new Railway project → **Deploy from GitHub**.
3. Add `BOT_TOKEN` as an environment variable.
4. Railway detects Python automatically. Set start command to `python brewbot.py`.

### Render

1. Create a new **Background Worker** on Render.
2. Connect your GitHub repo.
3. Set build command: `pip install -r requirements_brewbot.txt`
4. Set start command: `python brewbot.py`
5. Add `BOT_TOKEN` in the Environment tab.

### VPS (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install python3.11 python3.11-venv -y
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements_brewbot.txt

# Run with systemd or screen/tmux
python brewbot.py
```

**Systemd service (optional):**

```ini
# /etc/systemd/system/brewbot.service
[Unit]
Description=TheBrewBot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/tb-telegram-bot
ExecStart=/path/to/tb-telegram-bot/venv/bin/python brewbot.py
Restart=always
EnvironmentFile=/path/to/tb-telegram-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable brewbot && sudo systemctl start brewbot
```

## Database

SQLite file `cafebot.db` is created automatically with two tables:

- **calls** — every submitted call (chat, user, CA, thesis, type, initial MC, timestamp)
- **active_chats** — groups that have used `/cafeboard` or `/refresh` (for daily auto-posts)

## Commands

| Command | Where | Description |
|---------|-------|-------------|
| `/start` | Private | Welcome message |
| `/pnl <CA>` | Group | Generate your Coffee Card for a call |
| `/cafeboard` | Group | Show the group leaderboard |
| `/refresh` | Group | Force-refresh leaderboard with live data |

## License

MIT
