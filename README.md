# @SOLPoker_bot — Spin & Gold Texas Hold'em on Telegram

A production-ready Telegram bot for Texas Hold'em poker using Solana (SOL) for all buy-ins and payouts, modelled after GG Poker's Spin & Gold format.

## Features

- **3-max and 6-max tables** with configurable USD buy-ins ($1–$100)
- **Spin & Gold** style prize-pool randomisation with dynamic probability engine
- **Full Texas Hold'em** engine with hyper-turbo blind structure
- **Solana-native**: all deposits and payouts in SOL (no USDC/USDT)
- **Live SOL price** conversion from Jupiter, CoinGecko, or Binance
- **On-chain deposit verification** via Solana RPC
- **Automatic payouts** to winners' registered wallets
- **Jackpot reserve system** that starts from $0 and builds organically
- **Multi-group support** — runs in any number of Telegram groups simultaneously
- **Privacy-first**: wallets and cards only in private DMs

## Project Structure

```
bot.py             — Main bot: handlers, lobby, game orchestration
poker_engine.py    — Full Texas Hold'em engine (deck, evaluation, side pots)
blockchain.py      — Solana RPC: price fetch, deposit verify, SOL payouts
database.py        — Async SQLite layer (users, lobbies, games, jackpot)
spin_logic.py      — Multiplier tables, probability engine, spin animation
requirements.txt   — Python dependencies
.env.example       — Environment variable template
Procfile           — Railway / Heroku process file
railway.toml       — Railway deployment config
```

## Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd solpoker-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather |
| `MAIN_WALLET_ADDRESS` | Solana public key that receives all buy-ins |
| `CREATOR_WALLET_ADDRESS` | Creator wallet for profit allocation (private) |
| `HOT_WALLET_PRIVATE_KEY` | Base58-encoded full keypair for sending payouts |
| `SOLANA_RPC_URL` | Solana RPC endpoint (mainnet) |

### 3. Run

```bash
python bot.py
```

### 4. Deploy to Railway

The included `railway.toml` and `Procfile` are pre-configured:
- Set all env vars in the Railway dashboard
- Deploy from GitHub or CLI

## How It Works

### Game Flow

1. User sends `/spin` in a group chat
2. Selects table size (3 or 6 players) and USD buy-in
3. A lobby is posted with a "Join Game" button
4. Each player receives a DM with wallet warning and exact SOL payment amount
5. Bot polls Solana RPC every ~10 seconds for deposits
6. Once all players are confirmed → spin animation reveals the prize pool
7. Texas Hold'em begins with hyper-turbo blinds (3-minute levels)
8. Hole cards are dealt via DM; all actions happen in the group
9. Tournament continues until one player remains (3-max) or payouts are distributed
10. Winnings are automatically sent on-chain in SOL

### Commands

**Group chat:**
- `/spin` — Create a new game
- `/fold` `/check` `/call` — Poker actions
- `/bet X` `/raise X` — Bet or raise an amount
- `/allin` — Go all in
- `/status` — Show current table state

**Private chat:**
- `/start` — Register your Solana wallet
- `/editwallet` — Change your registered wallet

### Prize Logic

- Prize pool is determined by a dynamic probability engine
- 60–75% of spins result in sub-break-even prize pools (house profit)
- 20–30% of spins are near break-even
- 5–10% exceed buy-ins, funded by the jackpot reserve
- Surplus is split: 70% to jackpot reserve, 30% to creator wallet
- Jackpot and creator allocations are never publicly visible

## Security Notes

> **HOT WALLET RISKS**
>
> The `HOT_WALLET_PRIVATE_KEY` controls all payouts.
> - Store it in environment variables only — **never** commit to git
> - Use a dedicated wallet with limited funds
> - Regularly sweep profits to a cold wallet
> - Monitor for unauthorised transactions
> - Consider a multi-sig setup for production at scale

- All sensitive data (wallets, cards) is communicated only via private DM
- Bot tokens and keys must be kept in `.env` (gitignored)
- SQLite database contains wallet mappings — keep backups secure
- Rate limiting is applied to prevent abuse

## Tech Stack

- Python 3.11+
- aiogram 3.x (async Telegram framework)
- aiosqlite (async SQLite)
- solders (Solana transaction building)
- aiohttp (HTTP client for RPC & price APIs)

## License

Private / Internal — Not for redistribution.
