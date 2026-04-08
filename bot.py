#!/usr/bin/env python3
"""
☕ TheBrewBot — The Coffee-Themed Crypto Call Tracker
A cozy crypto-café Telegram bot that lets users "brew" calls and flex gains.
Python 3.11+ | python-telegram-bot 20.x | SQLite | Playwright | DexScreener
"""

import os
import re
import io
import html as _html
import time
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Use persistent volume if available (/data on Railway), else fall back to local
_data_dir = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_data_dir, "cafebot.db")
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={}"
CACHE_TTL = 45  # seconds
DAILY_INTERVAL = 86400  # 24 h in seconds
TOP_N = 15  # cafeboard size

# Regex patterns for contract addresses
ETH_CA_RE = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_CA_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BrewBot")

# ---------------------------------------------------------------------------
# In-memory DexScreener cache  {ca_lower: (timestamp, data_dict)}
# ---------------------------------------------------------------------------
_dex_cache: dict[str, tuple[float, dict]] = {}

# Temporary storage for pending calls  {message_id: {...}}
_pending_calls: dict[int, dict] = {}


# =========================================================================
# Database helpers
# =========================================================================
def _get_db() -> sqlite3.Connection:
    """Return a connection with WAL mode enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """Create tables on first run."""
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT    NOT NULL,
                ca          TEXT    NOT NULL,
                thesis      TEXT    NOT NULL,
                call_type   TEXT    NOT NULL,
                initial_mc  REAL    NOT NULL,
                timestamp   TEXT    NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_chats (
                chat_id              INTEGER PRIMARY KEY,
                last_board_timestamp TEXT
            );
            """
        )
        conn.commit()
    logger.info("Database initialized at %s", DB_PATH)


def seed_restored_calls() -> None:
    """One-time restore of calls lost during the accidental deployment incident."""
    CHAT_ID = -1003575636670
    restored = [
        {
            "username": "MrOK_777",
            "ca": "0x8e729198d1C59B82bd6bBa579310C40d740A11C2",
            "thesis": "Alvara ($ALVA)",
            "call_type": "alpha",
            "initial_mc": 1_690_000.0,
            "timestamp": "2026-04-07T15:06:00+00:00",  # 16:06 UK (BST = UTC+1)
        },
        {
            "username": "DonPJalapeno",
            "ca": "9JbxQSKNukRA7cPZxCfhSNEcAP9iKRo3PSyYNbW4pump",
            "thesis": "Momo ($MOMO)",
            "call_type": "alpha",
            "initial_mc": 51_970.0,
            "timestamp": "2026-04-07T13:58:00+00:00",  # 14:58 UK (BST = UTC+1)
        },
    ]
    with _get_db() as conn:
        for r in restored:
            existing = conn.execute(
                "SELECT 1 FROM calls WHERE chat_id=? AND LOWER(ca)=LOWER(?) LIMIT 1",
                (CHAT_ID, r["ca"]),
            ).fetchone()
            if existing:
                continue
            # Use negative hash of username as temp user_id until real user interacts
            temp_uid = -(abs(hash(r["username"])) % (10**9))
            conn.execute(
                "INSERT INTO calls (chat_id, user_id, username, ca, thesis, call_type, initial_mc, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (CHAT_ID, temp_uid, r["username"], r["ca"], r["thesis"],
                 r["call_type"], r["initial_mc"], r["timestamp"]),
            )
            logger.info("Restored call: @%s — %s", r["username"], r["ca"])
        conn.commit()


def reconcile_username(real_user_id: int, username: str) -> None:
    """If a user has restored records with a temp user_id, update them to the real ID."""
    if not username:
        return
    with _get_db() as conn:
        conn.execute(
            "UPDATE calls SET user_id=? WHERE user_id < 0 AND LOWER(username)=LOWER(?)",
            (real_user_id, username),
        )
        conn.commit()


def insert_call(
    chat_id: int,
    user_id: int,
    username: str,
    ca: str,
    thesis: str,
    call_type: str,
    initial_mc: float,
) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO calls (chat_id, user_id, username, ca, thesis, call_type, initial_mc, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                user_id,
                username,
                ca,
                thesis,
                call_type,
                initial_mc,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_user_call(chat_id: int, user_id: int, ca: str) -> Optional[dict]:
    """Fetch the first call a user made for this CA in a chat."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE chat_id=? AND user_id=? AND LOWER(ca)=LOWER(?) ORDER BY id ASC LIMIT 1",
            (chat_id, user_id, ca),
        ).fetchone()
    return dict(row) if row else None


def get_chat_calls(chat_id: int) -> list[dict]:
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM calls WHERE chat_id=? ORDER BY id ASC", (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_active_chat(chat_id: int) -> None:
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO active_chats (chat_id, last_board_timestamp) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_board_timestamp=excluded.last_board_timestamp",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_active_chats() -> list[int]:
    with _get_db() as conn:
        rows = conn.execute("SELECT chat_id FROM active_chats").fetchall()
    return [r["chat_id"] for r in rows]


# =========================================================================
# DexScreener helpers
# =========================================================================
def get_dexscreener_data(ca: str) -> Optional[dict]:
    """
    Fetch the highest-liquidity pair for *ca* from DexScreener.
    Returns dict with keys: symbol, name, fdv, liquidity_usd, volume_24h, pair_address
    Uses a short in-memory cache to stay rate-limit friendly.
    """
    key = ca.lower()
    now = time.time()
    cached = _dex_cache.get(key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    url = DEXSCREENER_SEARCH.format(ca)
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("DexScreener request failed for %s: %s", ca, exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Find the pair with highest liquidity where our CA is one of the tokens
    best = None
    best_liq = -1.0
    for p in pairs:
        liq = float(p.get("liquidity", {}).get("usd") or 0)
        # Match the CA to either base or quote token
        base_addr = (p.get("baseToken") or {}).get("address", "").lower()
        quote_addr = (p.get("quoteToken") or {}).get("address", "").lower()
        if key in (base_addr, quote_addr) and liq > best_liq:
            best = p
            best_liq = liq

    if best is None:
        # Fallback: highest liquidity pair overall
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))

    # Determine which token matches our CA
    base_addr = (best.get("baseToken") or {}).get("address", "").lower()
    if base_addr == key:
        token = best["baseToken"]
    else:
        token = best.get("quoteToken") or best.get("baseToken") or {}

    result = {
        "symbol": token.get("symbol", "???"),
        "name": token.get("name", "Unknown"),
        "fdv": float(best.get("fdv") or 0),
        "liquidity_usd": float(best.get("liquidity", {}).get("usd") or 0),
        "volume_24h": float(best.get("volume", {}).get("h24") or 0),
        "pair_address": best.get("pairAddress", ""),
    }
    _dex_cache[key] = (now, result)
    return result


# =========================================================================
# Formatting helpers
# =========================================================================
def fmt_usd(n: float) -> str:
    """Pretty-print a USD value with $ sign."""
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.2f}K"
    return f"${n:,.2f}"


def fmt_mcap(n: float) -> str:
    """Pretty-print a market cap value without $ sign (for card display)."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:,.2f}"


def is_valid_thesis(text: str) -> bool:
    """
    Check if a thesis is genuine and not spam/gibberish.
    Rejects: repeated characters, single-char spam, keyboard mashing,
    no real words, or insufficient word variety.
    """
    t = text.strip()
    if len(t) < 20:
        return False

    # Reject if any single character makes up >60% of the text
    from collections import Counter
    counts = Counter(t.lower().replace(" ", ""))
    total_chars = sum(counts.values())
    if total_chars > 0:
        most_common_char, most_common_count = counts.most_common(1)[0]
        if most_common_count / total_chars > 0.60:
            return False

    # Reject if fewer than 3 unique characters (excluding spaces)
    unique_chars = set(t.lower().replace(" ", ""))
    if len(unique_chars) < 4:
        return False

    # Must have at least 3 words
    words = t.split()
    if len(words) < 3:
        return False

    # At least 3 unique words
    unique_words = set(w.lower() for w in words)
    if len(unique_words) < 3:
        return False

    # Reject if average word length is < 2 (single char spam with spaces)
    avg_word_len = sum(len(w) for w in words) / len(words)
    if avg_word_len < 2:
        return False

    # Reject if most words are just repeated single characters (e.g. "aaaa bbbb cccc")
    spam_words = sum(1 for w in words if len(set(w.lower())) <= 1)
    if spam_words / len(words) > 0.5:
        return False

    return True


def pct_str(x: float) -> str:
    pct = (x - 1) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def x_str(x: float) -> str:
    return f"{x:.1f}x"


# =========================================================================
# Coffee Card Image Generation (Playwright) — v14 TradingBrew design
# =========================================================================
CARD_W, CARD_H = 1000, 600  # viewport; output is 2× (2000×1200)

def _e(t):
    """HTML-escape helper."""
    return _html.escape(str(t))

COFFEE_MUG_SVG = """
<svg viewBox="0 0 200 220" xmlns="http://www.w3.org/2000/svg">
  <path d="M75 65 Q78 45 72 28 Q68 15 75 2" fill="none" stroke="#c8956c" stroke-width="3" stroke-linecap="round" opacity="0.7">
    <animate attributeName="d" dur="3s" repeatCount="indefinite"
      values="M75 65 Q78 45 72 28 Q68 15 75 2;M75 65 Q70 45 76 28 Q80 15 73 2;M75 65 Q78 45 72 28 Q68 15 75 2"/>
  </path>
  <path d="M100 58 Q104 38 98 20 Q94 8 100 -5" fill="none" stroke="#c8956c" stroke-width="3" stroke-linecap="round" opacity="0.5">
    <animate attributeName="d" dur="2.5s" repeatCount="indefinite"
      values="M100 58 Q104 38 98 20 Q94 8 100 -5;M100 58 Q96 38 102 20 Q106 8 99 -5;M100 58 Q104 38 98 20 Q94 8 100 -5"/>
  </path>
  <path d="M125 62 Q128 42 122 25 Q118 12 125 0" fill="none" stroke="#c8956c" stroke-width="3" stroke-linecap="round" opacity="0.6">
    <animate attributeName="d" dur="3.5s" repeatCount="indefinite"
      values="M125 62 Q128 42 122 25 Q118 12 125 0;M125 62 Q122 42 128 25 Q132 12 124 0;M125 62 Q128 42 122 25 Q118 12 125 0"/>
  </path>
  <path d="M45 75 L45 150 Q45 175 70 175 L130 175 Q155 175 155 150 L155 75 Z"
        fill="#f0f0f0" stroke="#e0e0e0" stroke-width="1"/>
  <ellipse cx="100" cy="85" rx="50" ry="12" fill="#8B6544"/>
  <ellipse cx="100" cy="85" rx="50" ry="12" fill="url(#coffeeGrad)" opacity="0.6"/>
  <ellipse cx="82" cy="84" rx="6" ry="4" fill="#5C3D2E" opacity="0.5" transform="rotate(-15 82 84)"/>
  <ellipse cx="115" cy="86" rx="5" ry="3.5" fill="#5C3D2E" opacity="0.4" transform="rotate(10 115 86)"/>
  <ellipse cx="100" cy="75" rx="55" ry="12" fill="none" stroke="#ffffff" stroke-width="2" opacity="0.8"/>
  <ellipse cx="100" cy="75" rx="55" ry="12" fill="none" stroke="#d0d0d0" stroke-width="1"/>
  <path d="M155 95 Q185 95 185 125 Q185 155 155 155"
        fill="none" stroke="#f0f0f0" stroke-width="8" stroke-linecap="round"/>
  <path d="M155 95 Q185 95 185 125 Q185 155 155 155"
        fill="none" stroke="#e8e8e8" stroke-width="6" stroke-linecap="round"/>
  <path d="M45 75 L45 150 Q45 175 70 175 L130 175 Q155 175 155 150 L155 75 Z"
        fill="url(#cupShade)" opacity="0.15"/>
  <g transform="translate(70, 105)" opacity="0.25">
    <rect x="0" y="20" width="4" height="25" fill="#141820"/>
    <line x1="2" y1="15" x2="2" y2="50" stroke="#141820" stroke-width="1"/>
    <rect x="12" y="10" width="4" height="20" fill="#141820"/>
    <line x1="14" y1="5" x2="14" y2="35" stroke="#141820" stroke-width="1"/>
    <rect x="24" y="25" width="4" height="15" fill="#141820"/>
    <line x1="26" y1="18" x2="26" y2="45" stroke="#141820" stroke-width="1"/>
    <rect x="36" y="5" width="4" height="30" fill="#141820"/>
    <line x1="38" y1="0" x2="38" y2="40" stroke="#141820" stroke-width="1"/>
    <rect x="48" y="15" width="4" height="20" fill="#141820"/>
    <line x1="50" y1="8" x2="50" y2="40" stroke="#141820" stroke-width="1"/>
  </g>
  <ellipse cx="100" cy="180" rx="75" ry="14" fill="#e8e8e8"/>
  <ellipse cx="100" cy="180" rx="75" ry="14" fill="none" stroke="#d0d0d0" stroke-width="1"/>
  <ellipse cx="100" cy="177" rx="55" ry="8" fill="none" stroke="#d8d8d8" stroke-width="1" opacity="0.5"/>
  <ellipse cx="100" cy="195" rx="60" ry="6" fill="#c8956c" opacity="0.04"/>
  <defs>
    <linearGradient id="coffeeGrad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#6B4226"/>
      <stop offset="50%" stop-color="#9B7044"/>
      <stop offset="100%" stop-color="#6B4226"/>
    </linearGradient>
    <linearGradient id="cupShade" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#000"/>
      <stop offset="50%" stop-color="transparent"/>
      <stop offset="100%" stop-color="#000"/>
    </linearGradient>
  </defs>
</svg>
"""


def generate_coffee_card_image(
    ticker: str,
    token_name: str,
    ca: str,
    username: str,
    thesis: str,
    pct_gain: float,
    x_mult: float,
    call_type: str,
    brewed_on: str,
    called_at_mcap: str = "",
    ath_mcap: str = "",
) -> io.BytesIO:
    """
    Generate a TradingBrew-branded Coffee Card PNG via Playwright.
    Renders at 2× device scale for crisp 2000×1200 output.
    """
    is_profit = x_mult >= 1.0
    bg = "#141820"
    brown = "#c8956c"
    brown_glow = "rgba(200,149,108,0.35)"
    brown_dim = "rgba(200,149,108,0.15)"
    white = "#f0f0f0"
    red = "#e05252"
    red_glow = "rgba(224,82,82,0.35)"

    accent = brown if is_profit else red
    accent_glow = brown_glow if is_profit else red_glow
    accent_dim = brown_dim if is_profit else "rgba(224,82,82,0.15)"

    badge_label = "ALPHA" if call_type != "gamble" else "GAMBLE"
    badge_color = brown if call_type != "gamble" else "#e0a030"

    sign = "+" if pct_gain >= 0 else ""
    ca_short = ca[:12] + "..." + ca[-8:] if len(ca) > 24 else ca

    page_html = f"""<!DOCTYPE html>
<html>
<head>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    width: {CARD_W}px; height: {CARD_H}px; overflow: hidden;
    background: {bg}; font-family: 'Inter', 'Space Grotesk', sans-serif;
  }}
  .bg {{
    position: absolute; inset: 0;
    background:
      radial-gradient(ellipse 55% 50% at 0% 100%, rgba(200,149,108,0.04) 0%, transparent 70%),
      radial-gradient(ellipse 45% 55% at 100% 0%, rgba(200,149,108,0.03) 0%, transparent 60%),
      linear-gradient(180deg, #161c26 0%, #111620 50%, #0e1218 100%);
  }}
  .card {{
    position: absolute; top: 16px; left: 16px; right: 16px; bottom: 16px;
    border: 1.5px solid rgba(200,149,108,0.2); border-radius: 16px;
    overflow: hidden; display: flex; flex-direction: column;
    padding: 28px 40px 24px;
  }}
  .topbar {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 4px; z-index: 10; }}
  .brand-area {{ display: flex; flex-direction: column; gap: 2px; }}
  .brand-label {{ font-size: 11px; font-weight: 600; color: rgba(240,240,240,0.4); letter-spacing: 3px; text-transform: uppercase; }}
  .brand-name {{ font-family: 'Space Grotesk', sans-serif; font-size: 24px; font-weight: 700; letter-spacing: 0.5px; }}
  .brand-name .part1 {{ color: {white}; }}
  .brand-name .part2 {{ color: {brown}; }}
  .call-badge {{ font-size: 11px; font-weight: 700; color: {badge_color}; letter-spacing: 2px; border: 1px solid {badge_color}; padding: 5px 16px; border-radius: 4px; text-transform: uppercase; }}
  .main {{ flex: 1; display: flex; gap: 0; z-index: 10; }}
  .left {{ flex: 0 0 360px; display: flex; flex-direction: column; justify-content: center; }}
  .divider {{ width: 40px; height: 2px; background: {brown}; opacity: 0.5; margin: 6px 0; box-shadow: 0 0 8px {brown_glow}; }}
  .ticker {{ font-family: 'Space Grotesk', sans-serif; font-size: 82px; font-weight: 700; color: {white}; line-height: 0.95; letter-spacing: -1px; }}
  .ca {{ font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 500; color: rgba(240,240,240,0.3); margin-top: 4px; }}
  .growth-label {{ font-size: 12px; font-weight: 600; color: rgba(240,240,240,0.4); letter-spacing: 3px; text-transform: uppercase; margin-top: 18px; }}
  .growth-value {{ font-family: 'Space Grotesk', sans-serif; font-size: 72px; font-weight: 700; color: {accent}; line-height: 1; letter-spacing: -2px; text-shadow: 0 0 30px {accent_glow}, 0 0 60px {accent_dim}; display: flex; align-items: baseline; gap: 8px; }}
  .growth-value .pct {{ font-size: 28px; font-weight: 600; opacity: 0.7; }}
  .center {{ flex: 0 0 220px; display: flex; align-items: center; justify-content: center; }}
  .mug {{ width: 180px; height: 200px; opacity: 0.85; filter: drop-shadow(0 4px 20px rgba(200,149,108,0.1)); }}
  .right {{ flex: 1; display: flex; flex-direction: column; justify-content: center; align-items: flex-end; gap: 20px; text-align: right; }}
  .data-item {{ display: flex; flex-direction: column; gap: 2px; align-items: flex-end; }}
  .data-label {{ font-size: 12px; font-weight: 600; color: rgba(240,240,240,0.35); letter-spacing: 2.5px; text-transform: uppercase; }}
  .data-value {{ font-family: 'Space Grotesk', sans-serif; font-size: 36px; font-weight: 700; color: {accent}; letter-spacing: 0.5px; }}
  .data-value.white {{ color: {white}; }}
  .bottombar {{ display: flex; justify-content: space-between; align-items: flex-end; margin-top: auto; padding-top: 12px; z-index: 10; }}
  .powered {{ display: flex; align-items: center; gap: 8px; background: rgba(200,149,108,0.08); border: 1px solid rgba(200,149,108,0.18); border-radius: 20px; padding: 6px 20px; }}
  .powered-label {{ font-size: 11px; font-weight: 600; color: rgba(240,240,240,0.45); letter-spacing: 2px; text-transform: uppercase; }}
  .powered-name {{ font-family: 'Space Grotesk', sans-serif; font-size: 14px; font-weight: 700; }}
  .powered-name .t {{ color: {white}; }}
  .powered-name .b {{ color: {brown}; }}
  .called-by {{ text-align: right; }}
  .called-by-label {{ font-size: 11px; font-weight: 600; color: rgba(240,240,240,0.35); letter-spacing: 2px; text-transform: uppercase; }}
  .called-by-name {{ font-family: 'Space Grotesk', sans-serif; font-size: 28px; font-weight: 700; color: {white}; }}
</style>
</head>
<body>
<div class="bg"></div>
<div class="card">
  <div class="topbar">
    <div class="brand-area">
      <div class="brand-label">BREWED WITH</div>
      <div class="brand-name"><span class="part1">Brew</span><span class="part2">Bot</span></div>
    </div>
    <div class="call-badge">{_e(badge_label)}</div>
  </div>
  <div class="main">
    <div class="left">
      <div class="divider"></div>
      <div class="ticker">{_e(ticker.upper())}</div>
      <div class="ca">{_e(ca_short)}</div>
      <div class="growth-label">GROWTH</div>
      <div class="growth-value">{x_mult:.1f}x <span class="pct">({sign}{pct_gain:.0f}%)</span></div>
    </div>
    <div class="center">
      <div class="mug">{COFFEE_MUG_SVG}</div>
    </div>
    <div class="right">
      <div class="data-item">
        <div class="data-label">CALLED AT</div>
        <div class="data-value">{_e(called_at_mcap or 'N/A')}</div>
      </div>
      <div class="data-item">
        <div class="data-label">ATH POST CALL</div>
        <div class="data-value">{_e(ath_mcap or 'N/A')}</div>
      </div>
      <div class="data-item">
        <div class="data-label">DATE CALLED</div>
        <div class="data-value white">{_e(brewed_on)}</div>
      </div>
    </div>
  </div>
  <div class="bottombar">
    <div class="powered">
      <span class="powered-label">POWERED BY</span>
      <span class="powered-name"><span class="t">Trading</span><span class="b">Brew</span></span>
    </div>
    <div class="called-by">
      <div class="called-by-label">CALLED BY</div>
      <div class="called-by-name">@{_e(username or 'Anon')}</div>
    </div>
  </div>
</div>
</body>
</html>"""

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": CARD_W, "height": CARD_H},
            device_scale_factor=2,
        )
        page.set_content(page_html, wait_until="networkidle")
        page.wait_for_timeout(2500)
        screenshot = page.screenshot(type="png")
        browser.close()

    buf = io.BytesIO(screenshot)
    buf.seek(0)
    return buf


# =========================================================================
# Cafeboard calculation
# =========================================================================
def calculate_cafeboard(chat_id: int) -> list[dict]:
    """
    For each user in the chat, find their highest current x-multiplier
    (live from DexScreener). Returns sorted list of dicts:
    [{username, best_x, ca}, ...] descending by best_x.
    """
    calls = get_chat_calls(chat_id)
    if not calls:
        return []

    # Group by user
    user_calls: dict[int, list[dict]] = {}
    for c in calls:
        user_calls.setdefault(c["user_id"], []).append(c)

    results: list[dict] = []
    for uid, ucalls in user_calls.items():
        best_x = 0.0
        best_username = ucalls[0]["username"]
        for c in ucalls:
            dex = get_dexscreener_data(c["ca"])
            if dex and c["initial_mc"] > 0:
                cur_x = dex["fdv"] / c["initial_mc"]
                if cur_x > best_x:
                    best_x = cur_x
                    best_username = c["username"]
        if best_x > 0:
            results.append({"username": best_username, "best_x": best_x})

    results.sort(key=lambda r: r["best_x"], reverse=True)
    return results[:TOP_N]


def format_cafeboard(entries: list[dict]) -> str:
    """Render the cafeboard as a chat message."""
    if not entries:
        return "🏆 Cafeboard ☕\n\nNo brewed calls yet — be the first barista! ☕"

    lines = ["🏆 Cafeboard ☕\n"]
    for i, e in enumerate(entries, 1):
        fire = " 🔥" if i <= 3 else ""
        lines.append(f"{i}. @{e['username']} — {e['best_x']:.1f}x{fire}")
    return "\n".join(lines)


# =========================================================================
# Caption generation
# =========================================================================
def generate_caption(username: str, x_mult: float, pct_gain: float) -> str:
    """Build a warm coffee-themed caption for the Coffee Card photo."""
    if x_mult >= 10:
        compliment = (
            f"☕ Brewed to perfection! Your call just hit {x_mult:.1f}x "
            f"— you're the real alpha barista of the café! 🔥"
        )
    elif x_mult >= 5:
        compliment = (
            f"☕ Now that's a strong brew! {x_mult:.1f}x gains — "
            f"the café smells like pure alpha today! 🔥"
        )
    elif x_mult >= 2:
        compliment = (
            f"☕ A solid double shot! {x_mult:.1f}x — "
            f"your portfolio is steaming nicely! ☕"
        )
    elif x_mult >= 1:
        compliment = (
            f"☕ The brew is warming up! {x_mult:.1f}x so far — "
            f"keep that grinder going, @{username}! ☕"
        )
    else:
        compliment = (
            f"☕ Even the best baristas have off-days. {x_mult:.1f}x for now — "
            f"the next cup will be stronger, @{username}! 💪"
        )

    promo = (
        "Want your personal Coffee Card? "
        "Join the @TradingBrew community and start sharing your alpha! ☕"
    )
    return f"{compliment}\n\n{promo}"


# =========================================================================
# Contract address detection
# =========================================================================
def detect_ca(text: str) -> Optional[str]:
    """Return the first Ethereum or Solana contract address found in text."""
    m = ETH_CA_RE.search(text)
    if m:
        return m.group(0)
    # For Solana, be stricter: must be 32-44 base58 chars, surrounded by whitespace/boundaries
    # We iterate tokens to avoid false positives on normal words
    for token in text.split():
        if SOL_CA_RE.fullmatch(token) and len(token) >= 32:
            return token
    return None


# =========================================================================
# Telegram handlers
# =========================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start in private chat."""
    if update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            "☕ Welcome to TheBrewBot!\n\n"
            "Add me to a group chat to start tracking crypto calls.\n"
            "Share a contract address with a thesis (20+ chars) and I'll brew it for you!\n\n"
            "Type /help for full instructions ☕"
        )


HELP_TEXT = (
    "☕ <b>BrewBot — How It Works</b> ☕\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>1️⃣ Share a Call</b>\n"
    "Drop a contract address in the group along with your thesis "
    "(minimum 20 characters, at least 3 real words — no spam).\n\n"
    "<b>2️⃣ Pick Your Brew</b>\n"
    "BrewBot detects the CA, pulls live data from DexScreener, "
    "and gives you two buttons:\n"
    "  • <b>Alpha 🏆</b> — You're confident in this one\n"
    "  • <b>Gamble 🎲</b> — It's a degen play\n\n"
    "<b>3️⃣ Check Your PnL</b>\n"
    "Run <code>/pnl &lt;contract_address&gt;</code> any time to see "
    "your gains. BrewBot generates a Coffee Card showing your "
    "multiplier, growth %, and call details.\n\n"
    "<b>4️⃣ Compete on the Cafeboard</b>\n"
    "Run <code>/cafeboard</code> to see who has the best calls "
    "in the group. Top callers get 🔥\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Commands:</b>\n"
    "<code>/pnl &lt;CA&gt;</code> — Coffee Card with your PnL\n"
    "<code>/cafeboard</code> — Group leaderboard\n"
    "<code>/refresh</code> — Refresh leaderboard with live data\n"
    "<code>/help</code> — This message\n\n"
    "Powered by @TradingBrew ☕"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — show usage instructions."""
    if not update.message:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def _send_welcome(chat_id: int, chat_title: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate example Coffee Card and send welcome message to a chat."""
    logger.info("Sending welcome to: %s (%s)", chat_title, chat_id)

    # Register chat for daily cafeboard
    upsert_active_chat(chat_id)

    # Generate an example Coffee Card
    example_buf = None
    try:
        example_buf = generate_coffee_card_image(
            ticker="BREW",
            token_name="BrewCoin",
            ca="B6e1sq9rTBmFCLP5mVPocdfn8cNjDJPE4mBBMhup4ump",
            username="YourName",
            thesis="Example call",
            pct_gain=842.0,
            x_mult=9.4,
            call_type="alpha",
            brewed_on="2026-01-15",
            called_at_mcap="45.20K",
            ath_mcap="425.88K",
        )
    except Exception as exc:
        logger.error("Failed to generate welcome card: %s", exc, exc_info=True)

    welcome_text = (
        "☕ <b>BrewBot has entered the café!</b> ☕\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "I'm your crypto call tracker. Share a contract address "
        "with your thesis and I'll lock in the market cap. "
        "Come back later to check your PnL with a Coffee Card.\n\n"
        "<b>How to use me:</b>\n\n"
        "<b>1.</b> Paste a contract address + your thesis (min 20 chars, real words)\n"
        "<b>2.</b> Tap <b>Alpha 🏆</b> or <b>Gamble 🎲</b> to lock in your call\n"
        "<b>3.</b> Run <code>/pnl &lt;CA&gt;</code> to see your gains on a Coffee Card\n"
        "<b>4.</b> Run <code>/cafeboard</code> to see who's the top caller\n\n"
        "Attached below is an example Coffee Card — "
        "this is what you'll get when you check your PnL ☕\n\n"
        "Type <code>/help</code> any time for full instructions.\n\n"
        "<i>Powered by @TradingBrew</i>"
    )

    if example_buf:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=example_buf,
            caption=welcome_text,
            parse_mode="HTML",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            parse_mode="HTML",
        )
    logger.info("Welcome message sent to chat %s", chat_id)


async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback: fires on new_chat_members status update (legacy path)."""
    if not update.message or not update.message.new_chat_members:
        return
    bot_added = any(m.id == context.bot.id for m in update.message.new_chat_members)
    if not bot_added:
        return
    chat = update.effective_chat
    if chat:
        await _send_welcome(chat.id, chat.title or "?", context)


async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Primary: fires on my_chat_member update when bot is added/promoted in a group."""
    if not update.my_chat_member:
        return
    old = update.my_chat_member.old_chat_member
    new = update.my_chat_member.new_chat_member
    # Detect transition from non-member/left/kicked → member/admin
    was_member = old.status in ("member", "administrator", "creator")
    is_member = new.status in ("member", "administrator", "creator")
    if was_member or not is_member:
        return  # not a "bot just joined" transition
    chat = update.my_chat_member.chat
    if chat:
        await _send_welcome(chat.id, chat.title or "?", context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan group messages for contract addresses."""
    if not update.message or not update.message.text:
        return
    chat = update.effective_chat
    if not chat or chat.type in ("private",):
        return

    # Reconcile temp user_ids from restored data
    user = update.message.from_user
    if user and user.username:
        reconcile_username(user.id, user.username)

    text = update.message.text

    # Ignore messages mentioning @HeyTB_bot — those are for a different bot
    if "@heytb_bot" in text.lower():
        return

    ca = detect_ca(text)
    if not ca:
        return

    # Extract thesis (original text minus the CA)
    thesis = text.replace(ca, "").strip()
    if len(thesis) < 20 or not is_valid_thesis(thesis):
        await update.message.reply_text(
            "☕ To submit a call, you need to provide a real thesis alongside "
            "the contract address — minimum 20 characters, at least 3 real words. "
            "No spam or gibberish! Give us your actual reasoning ☕"
        )
        return

    # Fetch DexScreener data
    dex = get_dexscreener_data(ca)
    if not dex:
        await update.message.reply_text(
            "☕ Couldn't find that token on DexScreener. "
            "Double-check the contract address and try again!"
        )
        return

    if dex["fdv"] <= 0:
        await update.message.reply_text(
            "☕ Token found but market cap data isn't available yet. Try again later!"
        )
        return

    # Build reply
    info_text = (
        f"☕ <b>{dex['name']}</b> (${dex['symbol']})\n\n"
        f"💰 Market Cap (FDV): <b>{fmt_usd(dex['fdv'])}</b>\n"
        f"💧 Liquidity: <b>{fmt_usd(dex['liquidity_usd'])}</b>\n"
        f"📊 24h Volume: <b>{fmt_usd(dex['volume_24h'])}</b>\n\n"
        f"<i>Pick your brew below ↓</i>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Gamble 🎲", callback_data=f"call:gamble:{ca}"),
                InlineKeyboardButton("Alpha 🏆", callback_data=f"call:alpha:{ca}"),
            ]
        ]
    )

    sent = await update.message.reply_text(
        info_text, parse_mode="HTML", reply_markup=keyboard
    )

    # Store pending call data keyed by the bot's reply message ID
    _pending_calls[sent.message_id] = {
        "ca": ca,
        "thesis": text,  # full original message text
        "initial_mc": dex["fdv"],
        "user_id": update.message.from_user.id,
        "username": update.message.from_user.username or update.message.from_user.first_name or "Anon",
        "chat_id": chat.id,
        "dex": dex,
    }


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Gamble / Alpha button presses."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "call":
        return

    call_type = parts[1]  # "gamble" or "alpha"
    ca = parts[2]

    msg_id = query.message.message_id
    pending = _pending_calls.get(msg_id)
    if not pending:
        await query.answer("☕ This call has already been brewed or expired!", show_alert=True)
        return

    # Only the original caller can submit
    clicker = query.from_user
    if clicker.id != pending["user_id"]:
        await query.answer("☕ Only the original caller can brew this one!", show_alert=True)
        return

    # Check for duplicate
    existing = get_user_call(pending["chat_id"], pending["user_id"], ca)
    if existing:
        await query.answer("☕ You've already brewed a call for this token!", show_alert=True)
        return

    # Insert into DB
    insert_call(
        chat_id=pending["chat_id"],
        user_id=pending["user_id"],
        username=pending["username"],
        ca=ca,
        thesis=pending["thesis"],
        call_type=call_type,
        initial_mc=pending["initial_mc"],
    )

    # Clean up pending
    del _pending_calls[msg_id]

    type_label = "Gamble 🎲" if call_type == "gamble" else "Alpha 🏆"
    dex = pending["dex"]
    confirm_text = (
        f"☕ <b>{dex['name']}</b> (${dex['symbol']})\n\n"
        f"💰 Market Cap (FDV): <b>{fmt_usd(dex['fdv'])}</b>\n"
        f"💧 Liquidity: <b>{fmt_usd(dex['liquidity_usd'])}</b>\n"
        f"📊 24h Volume: <b>{fmt_usd(dex['volume_24h'])}</b>\n\n"
        f"✅ Call submitted as <b>{type_label}</b>! ☕ Initial MC locked in."
    )

    await query.edit_message_text(text=confirm_text, parse_mode="HTML")
    logger.info(
        "Call submitted: user=%s type=%s ca=%s mc=%s",
        pending["username"],
        call_type,
        ca,
        pending["initial_mc"],
    )


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pnl <contract_address>."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /pnl in a group chat!")
        return

    text = update.message.text or ""
    ca = detect_ca(text)
    if not ca:
        await update.message.reply_text(
            "☕ Please provide a contract address: /pnl <contract_address>"
        )
        return

    user = update.message.from_user
    call = get_user_call(chat.id, user.id, ca)
    if not call:
        await update.message.reply_text(
            "☕ No brewed call found for that token in this chat. "
            "Share a CA with your thesis first!"
        )
        return

    # Fetch current data
    dex = get_dexscreener_data(ca)
    if not dex:
        await update.message.reply_text(
            "☕ Couldn't fetch current data from DexScreener. Try again shortly!"
        )
        return

    current_mc = dex["fdv"]
    initial_mc = call["initial_mc"]
    if initial_mc <= 0:
        await update.message.reply_text("☕ Initial market cap was zero — can't calculate PnL!")
        return

    x_mult = current_mc / initial_mc
    pct_gain = (x_mult - 1) * 100

    # Parse timestamp for "Brewed on" — short YYYY-MM-DD format
    try:
        ts = datetime.fromisoformat(call["timestamp"])
        brewed_on = ts.strftime("%Y-%m-%d")
    except Exception:
        brewed_on = call["timestamp"]

    username = call["username"]

    # Format mcap values for the card (no $ sign)
    called_at_mcap = fmt_mcap(initial_mc)
    ath_mcap = fmt_mcap(current_mc) if x_mult >= 1 else fmt_mcap(initial_mc)

    # Generate Coffee Card
    card_buf = generate_coffee_card_image(
        ticker=dex["symbol"],
        token_name=dex["name"],
        ca=ca,
        username=username,
        thesis=call["thesis"],
        pct_gain=pct_gain,
        x_mult=x_mult,
        call_type=call["call_type"],
        brewed_on=brewed_on,
        called_at_mcap=called_at_mcap,
        ath_mcap=ath_mcap,
    )

    caption = generate_caption(username, x_mult, pct_gain)

    await update.message.reply_photo(photo=card_buf, caption=caption)
    logger.info("PnL card sent for user=%s ca=%s x=%.1f", username, ca, x_mult)


async def cafeboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cafeboard — show the leaderboard."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /cafeboard in a group chat!")
        return

    # Register chat for daily auto-posts
    upsert_active_chat(chat.id)

    await update.message.reply_text("☕ Brewing the Cafeboard… hang tight!")

    entries = calculate_cafeboard(chat.id)
    board_text = format_cafeboard(entries)
    await update.message.reply_text(board_text)
    logger.info("Cafeboard posted for chat %s (%d entries)", chat.id, len(entries))


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /refresh — recalculate and post the cafeboard."""
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("☕ Use /refresh in a group chat!")
        return

    upsert_active_chat(chat.id)

    # Clear cache to force fresh data
    _dex_cache.clear()

    await update.message.reply_text("☕ Refreshing the Cafeboard with fresh beans…")

    entries = calculate_cafeboard(chat.id)
    board_text = format_cafeboard(entries)
    await update.message.reply_text(board_text)
    logger.info("Cafeboard refreshed for chat %s (%d entries)", chat.id, len(entries))


# =========================================================================
# Daily auto-post job
# =========================================================================
async def daily_cafeboard_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-post the cafeboard to all active groups every 24 hours."""
    logger.info("Running daily cafeboard auto-post…")
    active = get_active_chats()
    for chat_id in active:
        try:
            # Clear cache per-chat to get fresh data
            _dex_cache.clear()
            entries = calculate_cafeboard(chat_id)
            board_text = "☕ Daily Cafeboard Update!\n\n" + format_cafeboard(entries)
            await context.bot.send_message(chat_id=chat_id, text=board_text)
            upsert_active_chat(chat_id)
            logger.info("Daily board posted to chat %s", chat_id)
        except Exception as exc:
            logger.warning("Failed to post daily board to %s: %s", chat_id, exc)


# =========================================================================
# Main
# =========================================================================
def main() -> None:
    """Start TheBrewBot."""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set — create a .env file with BOT_TOKEN=your_token")
        return

    init_db()
    seed_restored_calls()

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pnl", pnl_command))
    app.add_handler(CommandHandler("cafeboard", cafeboard_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Welcome handler — primary: my_chat_member update (reliable)
    app.add_handler(
        ChatMemberHandler(chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    # Welcome handler — fallback: new_chat_members status update (legacy)
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_handler)
    )
    # Message handler — must be last to avoid swallowing commands
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_message)
    )

    # Schedule daily cafeboard job (first run 24h after startup)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(
            daily_cafeboard_job,
            interval=DAILY_INTERVAL,
            first=DAILY_INTERVAL,
            name="daily_cafeboard",
        )
        logger.info("Daily cafeboard job scheduled (every %ds)", DAILY_INTERVAL)
    else:
        logger.warning("JobQueue not available — daily auto-post disabled")

    logger.info("☕ TheBrewBot is starting up…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,  # includes my_chat_member
    )


if __name__ == "__main__":
    main()
