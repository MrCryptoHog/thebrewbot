#!/usr/bin/env python3
"""
bot.py — @SOLPoker_bot  |  Texas Hold'em Spin & Gold on Telegram + Solana.

Handles: lobby creation (/spin), wallet registration, deposit polling,
spin animation, poker gameplay, and on-chain payouts.

Python 3.11+  •  aiogram 3.x  •  aiosqlite  •  solders
"""

import asyncio
import logging
import os
import re
import time
import uuid
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database as db
import blockchain as bc
import spin_logic
from poker_engine import PokerGame, PlayerState, card_display

# ─── Config ──────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAIN_WALLET = os.getenv("MAIN_WALLET_ADDRESS", "")
CREATOR_WALLET = "7N6uj5Eg66MEQZDshhRjbGfPjoyK1DAKAYj9cBcSvhnh"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ─── In-memory state ─────────────────────────────────────────────────────
# pending_spins: {creator_telegram_id: {chat_id, message_id, max_players}}
pending_spins: dict[int, dict] = {}

# active_games: {chat_id: PokerGame}
active_games: dict[int, PokerGame] = {}

# deposit poll tasks: {lobby_id: asyncio.Task}
deposit_tasks: dict[str, asyncio.Task] = {}

# turn timers: {game_id: asyncio.Task}
turn_timers: dict[str, asyncio.Task] = {}

BUYIN_OPTIONS = [1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0]
MIN_BUYIN = 1.0  # minimum custom buy-in in USD
TURN_TIMEOUT = 45  # seconds per action
LOBBY_TIMEOUT = 1800  # 30 min before lobby expires

SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# ═══════════════════════════════════════════════════════════════════════════
# /start  (private chat — registration)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    user = await db.get_user(message.from_user.id)
    text = (
        "♠️ <b>Welcome to SOL Poker — Spin & Gold</b> ♠️\n\n"
        "All wallet and card communication happens <b>only here in private chat</b>.\n"
        "Group chats show lobby, spin, gameplay, and results only.\n\n"
        "🔗 <b>This bot operates exclusively with native SOL on Solana.</b>\n"
        "No USDC, USDT, or any other token is accepted.\n"
        "No refunds are supported.\n\n"
    )
    if user and user.get("wallet_address"):
        text += (
            f"✅ Your registered wallet:\n<code>{user['wallet_address']}</code>\n\n"
            "Use /editwallet to change it."
        )
    else:
        text += (
            "To play, register your Solana wallet address.\n"
            "Simply send your wallet address as a message here."
        )
    await message.answer(text)


# ═══════════════════════════════════════════════════════════════════════════
# Wallet registration (private text)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(SOL_ADDR_RE))
async def register_wallet(message: Message) -> None:
    wallet = message.text.strip()
    user = await db.get_user(message.from_user.id)
    if user and user.get("wallet_address"):
        await message.answer(
            f"You already have a wallet registered:\n<code>{user['wallet_address']}</code>\n"
            "Use /editwallet to change it."
        )
        return
    await db.register_user(
        message.from_user.id,
        message.from_user.username or message.from_user.first_name,
        wallet,
    )
    await message.answer(f"✅ Wallet registered:\n<code>{wallet}</code>")


# ═══════════════════════════════════════════════════════════════════════════
# /editwallet  (private chat)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(Command("editwallet"), F.chat.type == ChatType.PRIVATE)
async def cmd_editwallet(message: Message) -> None:
    # Check if user is in an active lobby/game
    in_lobby = await db.player_in_any_active_lobby(message.from_user.id)
    if in_lobby:
        await message.answer("⚠️ You cannot change your wallet while in an active game.")
        return
    await message.answer("Send your new Solana wallet address:")
    # Set a flag so the next message is treated as a wallet update
    pending_spins.setdefault(-message.from_user.id, {"edit_wallet": True})


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_text_handler(message: Message) -> None:
    """Catch-all for private text — handle wallet edits & unknown input."""
    uid = message.from_user.id
    flag = pending_spins.get(-uid)
    if flag and flag.get("edit_wallet"):
        wallet = (message.text or "").strip()
        if not SOL_ADDR_RE.match(wallet):
            await message.answer("❌ Invalid Solana address. Please try again.")
            return
        await db.update_wallet(uid, wallet)
        pending_spins.pop(-uid, None)
        await message.answer(f"✅ Wallet updated to:\n<code>{wallet}</code>")
        return
    # Unknown text
    user = await db.get_user(uid)
    if not user or not user.get("wallet_address"):
        # Try to register
        wallet = (message.text or "").strip()
        if SOL_ADDR_RE.match(wallet):
            await db.register_user(
                uid,
                message.from_user.username or message.from_user.first_name,
                wallet,
            )
            await message.answer(f"✅ Wallet registered:\n<code>{wallet}</code>")
            return
    await message.answer("Use /start for help or send a valid Solana address to register.")


# ═══════════════════════════════════════════════════════════════════════════
# /spin  (group chat — game creation)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(Command("spin"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_spin(message: Message) -> None:
    uid = message.from_user.id

    # Check wallet registered
    user = await db.get_user(uid)
    if not user or not user.get("wallet_address"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Register wallet →", url=f"https://t.me/{(await bot.me()).username}?start=register")]
        ])
        await message.answer(
            "⚠️ You must register a wallet first.\n"
            "Click below to open a private chat with the bot.",
            reply_markup=kb,
        )
        return

    # Check not already in a game
    existing = await db.player_in_any_active_lobby(uid)
    if existing:
        await message.answer("⚠️ You're already in game. Finish it first.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="3 Players", callback_data=f"spin_size:3:{uid}"),
            InlineKeyboardButton(text="6 Players", callback_data=f"spin_size:6:{uid}"),
        ]
    ])
    msg = await message.answer("♠️ <b>New Spin & Gold</b>\nSelect table size:", reply_markup=kb)
    pending_spins[uid] = {"chat_id": message.chat.id, "message_id": msg.message_id}


@router.callback_query(F.data.startswith("spin_size:"))
async def on_spin_size(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    max_players = int(parts[1])
    creator_id = int(parts[2])

    if cb.from_user.id != creator_id:
        await cb.answer("Only the game creator can choose.", show_alert=True)
        return

    pending_spins[creator_id]["max_players"] = max_players

    rows = []
    row = []
    for amt in BUYIN_OPTIONS:
        label = f"${amt:g}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"spin_buyin:{amt}:{creator_id}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Custom buy-in button for whales / high-stakes
    rows.append([InlineKeyboardButton(text="💎 Custom Amount", callback_data=f"custom_buyin:{creator_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.edit_text(
        f"♠️ <b>Spin & Gold — {max_players}-Max</b>\n"
        "Select buy-in amount (USD equivalent, paid in SOL):",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("custom_buyin:"))
async def on_custom_buyin(cb: CallbackQuery) -> None:
    creator_id = int(cb.data.split(":")[1])
    if cb.from_user.id != creator_id:
        await cb.answer("Only the game creator can choose.", show_alert=True)
        return
    info = pending_spins.get(creator_id)
    if not info:
        await cb.answer("Session expired. Use /spin again.", show_alert=True)
        return
    info["awaiting_custom_buyin"] = True
    await cb.message.edit_text(
        f"♠️ <b>Spin & Gold — {info['max_players']}-Max</b>\n\n"
        f"💎 <b>Custom Buy-in</b>\n"
        f"Type your buy-in amount in USD (minimum ${MIN_BUYIN:g}).\n"
        f"Example: <code>500</code> or <code>2500</code>",
    )
    await cb.answer()


@router.callback_query(F.data.startswith("spin_buyin:"))
async def on_spin_buyin(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    buyin_usd = float(parts[1])
    creator_id = int(parts[2])

    if cb.from_user.id != creator_id:
        await cb.answer("Only the game creator can choose.", show_alert=True)
        return

    info = pending_spins.pop(creator_id, None)
    if not info:
        await cb.answer("Session expired. Use /spin again.", show_alert=True)
        return

    max_players = info["max_players"]
    chat_id = info["chat_id"]

    # Fetch SOL price and calculate buy-in
    async with aiohttp.ClientSession() as session:
        sol_price = await bc.get_sol_price(session)

    buyin_sol = bc.usd_to_sol(buyin_usd, sol_price)
    lobby_id = str(uuid.uuid4())[:12]

    # Create lobby in DB
    await db.create_lobby(
        lobby_id=lobby_id,
        chat_id=chat_id,
        creator_id=creator_id,
        max_players=max_players,
        buyin_usd=buyin_usd,
        buyin_sol=buyin_sol,
        sol_price=sol_price,
    )

    # Auto-join creator
    await db.add_lobby_player(lobby_id, creator_id)

    # Post lobby message
    lobby_text = await _render_lobby(lobby_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Join Game", callback_data=f"join:{lobby_id}")]
    ])
    msg = await bot.send_message(chat_id, lobby_text, reply_markup=kb)
    await db.update_lobby(lobby_id, message_id=msg.message_id)

    # Delete the setup message
    try:
        await cb.message.delete()
    except Exception:
        pass

    await cb.answer()

    # Send DM to creator with wallet warning + payment info
    await _send_payment_dm(creator_id, lobby_id, buyin_usd, buyin_sol, sol_price)

    # Start deposit polling
    task = asyncio.create_task(_poll_deposits(lobby_id))
    deposit_tasks[lobby_id] = task


# ═══════════════════════════════════════════════════════════════════════════
# Custom buy-in amount handler (group chat text)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text.regexp(r"^\d+(\.\d+)?$"))
async def on_custom_buyin_amount(message: Message) -> None:
    """Catch a plain number in group chat from a creator awaiting custom buy-in."""
    uid = message.from_user.id
    info = pending_spins.get(uid)
    if not info or not info.get("awaiting_custom_buyin"):
        return  # Not waiting for a custom amount — ignore silently

    try:
        buyin_usd = round(float(message.text.strip()), 2)
    except ValueError:
        await message.reply("❌ Enter a valid number (e.g. <code>500</code> or <code>2500</code>).")
        return

    if buyin_usd < MIN_BUYIN:
        await message.reply(f"❌ Minimum buy-in is ${MIN_BUYIN:g}.")
        return

    # Pop from pending and proceed (same logic as on_spin_buyin)
    info = pending_spins.pop(uid, None)
    if not info:
        await message.reply("Session expired. Use /spin again.")
        return

    max_players = info["max_players"]
    chat_id = info["chat_id"]

    async with aiohttp.ClientSession() as session:
        sol_price = await bc.get_sol_price(session)

    buyin_sol = bc.usd_to_sol(buyin_usd, sol_price)
    lobby_id = str(uuid.uuid4())[:12]

    await db.create_lobby(
        lobby_id=lobby_id,
        chat_id=chat_id,
        creator_id=uid,
        max_players=max_players,
        buyin_usd=buyin_usd,
        buyin_sol=buyin_sol,
        sol_price=sol_price,
    )

    await db.add_lobby_player(lobby_id, uid)

    lobby_text = await _render_lobby(lobby_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Join Game", callback_data=f"join:{lobby_id}")]
    ])
    msg = await bot.send_message(chat_id, lobby_text, reply_markup=kb)
    await db.update_lobby(lobby_id, message_id=msg.message_id)

    await _send_payment_dm(uid, lobby_id, buyin_usd, buyin_sol, sol_price)

    task = asyncio.create_task(_poll_deposits(lobby_id))
    deposit_tasks[lobby_id] = task


# ═══════════════════════════════════════════════════════════════════════════
# Join game callback
# ═══════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("join:"))
async def on_join(cb: CallbackQuery) -> None:
    lobby_id = cb.data.split(":")[1]
    uid = cb.from_user.id

    lobby = await db.get_lobby(lobby_id)
    if not lobby or lobby["status"] != "waiting":
        await cb.answer("This lobby is no longer available.", show_alert=True)
        return

    # Check wallet
    user = await db.get_user(uid)
    if not user or not user.get("wallet_address"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Register wallet →",
                                  url=f"https://t.me/{(await bot.me()).username}?start=register")]
        ])
        await cb.message.answer(
            "⚠️ You need to register a wallet before joining.\n"
            "Click below to start a private chat with the bot.",
            reply_markup=kb,
        )
        await cb.answer()
        return

    # Check already in a game
    existing = await db.player_in_any_active_lobby(uid)
    if existing:
        await cb.answer("You're already in an active game.", show_alert=True)
        return

    # Check lobby capacity
    count = await db.count_lobby_players(lobby_id)
    if count >= lobby["max_players"]:
        await cb.answer("Lobby is full!", show_alert=True)
        return

    # Check not already in this lobby
    if await db.player_in_lobby(lobby_id, uid):
        await cb.answer("You already joined this lobby.", show_alert=True)
        return

    await db.add_lobby_player(lobby_id, uid)

    # Update lobby message
    lobby_text = await _render_lobby(lobby_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Join Game", callback_data=f"join:{lobby_id}")]
    ])
    try:
        await bot.edit_message_text(
            lobby_text, chat_id=lobby["chat_id"],
            message_id=lobby["message_id"], reply_markup=kb,
        )
    except Exception:
        pass

    await cb.answer("Joined! Check your DMs for payment instructions.")

    # Send DM
    await _send_payment_dm(uid, lobby_id, lobby["buyin_usd"], lobby["buyin_sol"], lobby["sol_price_at_create"])


# ═══════════════════════════════════════════════════════════════════════════
# Lobby rendering
# ═══════════════════════════════════════════════════════════════════════════

async def _render_lobby(lobby_id: str) -> str:
    lobby = await db.get_lobby(lobby_id)
    players = await db.get_lobby_players(lobby_id)

    lines = [
        f"♠️ <b>Spin & Gold Lobby</b>  ({lobby['max_players']}-Max)",
        f"💵 Buy-in: ${lobby['buyin_usd']:g}  ({lobby['buyin_sol']:.6f} SOL)",
        f"🌐 Network: <b>Solana (SOL only)</b>",
        f"👥 {len(players)}/{lobby['max_players']} players",
        "",
    ]
    for p in players:
        icon = "✅" if p["status"] == "confirmed" else "⏳"
        status_text = "Buy In Confirmed" if p["status"] == "confirmed" else "Buy In Pending"
        lines.append(f"  {icon} @{p['username']} — {status_text}")

    lines.append("")
    lines.append(f"Lobby ID: <code>{lobby_id}</code>")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# DM helpers
# ═══════════════════════════════════════════════════════════════════════════

async def _send_payment_dm(
    telegram_id: int, lobby_id: str,
    buyin_usd: float, buyin_sol: float, sol_price: float,
) -> None:
    user = await db.get_user(telegram_id)
    wallet = user["wallet_address"] if user else "NOT SET"

    text = (
        "⚠️ <b>Wallet Warning Before You Play</b>\n\n"
        "Make sure you send the buy-in from your <b>registered</b> Solana wallet.\n"
        "If you want to use a different wallet, update it using /editwallet <b>before</b> playing.\n\n"
        f"Your current wallet:\n<code>{wallet}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Buy-in: <b>${buyin_usd:g}</b>\n"
        f"💰 SOL amount: <b>{buyin_sol:.6f} SOL</b>\n"
        f"📈 SOL price: ${sol_price:,.2f}\n\n"
        f"Send exactly <b>{buyin_sol:.6f} SOL</b> (${buyin_usd:g}) to the main wallet:\n\n"
        f"<code>{MAIN_WALLET}</code>\n\n"
        "🔴 <b>Only SOL on Solana is accepted.</b>\n"
        "No USDC, USDT, or any other token.\n"
        "Amounts less than the buy-in will be ignored.\n"
        "Overpayments will be accepted — excess is banked.\n"
        "<b>No refunds.</b>\n\n"
        f"Lobby: <code>{lobby_id}</code>"
    )
    try:
        await bot.send_message(telegram_id, text)
    except Exception as exc:
        logger.warning("Cannot DM user %d: %s  — they may not have started the bot.", telegram_id, exc)


async def _send_hole_cards_dm(player: PlayerState) -> None:
    """Send the player their hole cards privately."""
    cards = card_display(player.hole_cards)
    text = (
        "🃏 <b>Your Hole Cards</b>\n\n"
        f"  {cards}\n\n"
        "Good luck! Actions happen in the group chat."
    )
    try:
        await bot.send_message(player.telegram_id, text)
    except Exception as exc:
        logger.warning("Cannot DM hole cards to %d: %s", player.telegram_id, exc)


# ═══════════════════════════════════════════════════════════════════════════
# Deposit polling
# ═══════════════════════════════════════════════════════════════════════════

async def _poll_deposits(lobby_id: str) -> None:
    """Background task that checks for deposits every ~10 s."""
    start_time = time.time()
    try:
        while True:
            await asyncio.sleep(10)

            lobby = await db.get_lobby(lobby_id)
            if not lobby or lobby["status"] not in ("waiting",):
                return

            if time.time() - start_time > LOBBY_TIMEOUT:
                await db.update_lobby(lobby_id, status="expired")
                try:
                    await bot.edit_message_text(
                        "⏰ Lobby expired — not all buy-ins were received in time.",
                        chat_id=lobby["chat_id"],
                        message_id=lobby["message_id"],
                    )
                except Exception:
                    pass
                return

            players = await db.get_lobby_players(lobby_id)
            pending = [p for p in players if p["status"] == "pending"]
            if not pending:
                # All confirmed — start game
                break

            required_sol = lobby["buyin_sol"]

            async with aiohttp.ClientSession() as session:
                confirmed = await bc.check_deposits(session, pending, required_sol)

            for cp in confirmed:
                await db.update_player_status(lobby_id, cp["telegram_id"], "confirmed")
                await db.log_transaction(
                    lobby_id=lobby_id,
                    telegram_id=cp["telegram_id"],
                    tx_type="buyin",
                    amount_usd=lobby["buyin_usd"],
                    amount_sol=cp.get("received_sol", required_sol),
                    sol_price=lobby["sol_price_at_create"],
                    tx_signature=cp.get("tx_signature", ""),
                    notes=f"Received {cp.get('received_sol', 0):.6f} SOL",
                )

            if confirmed:
                # Update lobby message
                lobby_text = await _render_lobby(lobby_id)
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎮 Join Game", callback_data=f"join:{lobby_id}")]
                ])
                try:
                    await bot.edit_message_text(
                        lobby_text,
                        chat_id=lobby["chat_id"],
                        message_id=lobby["message_id"],
                        reply_markup=kb,
                    )
                except Exception:
                    pass

            # Re-check if all confirmed now
            players = await db.get_lobby_players(lobby_id)
            all_confirmed = all(p["status"] == "confirmed" for p in players)
            lobby_full = len(players) >= lobby["max_players"]

            if all_confirmed and lobby_full:
                break

    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.exception("Deposit polling error for lobby %s: %s", lobby_id, exc)
        return

    # All confirmed and full — start the game
    await _start_game(lobby_id)


# ═══════════════════════════════════════════════════════════════════════════
# Game start — spin animation + poker
# ═══════════════════════════════════════════════════════════════════════════

async def _start_game(lobby_id: str) -> None:
    lobby = await db.get_lobby(lobby_id)
    if not lobby:
        return
    await db.update_lobby(lobby_id, status="spinning")

    chat_id = lobby["chat_id"]
    buyin_usd = lobby["buyin_usd"]
    max_players = lobby["max_players"]

    # ── Spin ──────────────────────────────────────────────
    jackpot = await db.get_jackpot_balance()
    entry = spin_logic.spin(max_players, buyin_usd, jackpot)
    prize_pool = spin_logic.calculate_prize_pool(entry, buyin_usd)
    payouts = spin_logic.get_payout_amounts(entry, buyin_usd)
    allocation = spin_logic.compute_allocation(entry, buyin_usd, max_players)

    # Update reserves
    if allocation["jackpot_credit"] > 0:
        await db.update_jackpot_balance(allocation["jackpot_credit"])
        await db.log_transaction(lobby_id, 0, "jackpot_credit",
                                 allocation["jackpot_credit"], 0, 0,
                                 notes=f"Surplus from lobby {lobby_id}")
    if allocation["creator_credit"] > 0:
        # Send the 30 % creator split on-chain to the creator wallet
        try:
            async with aiohttp.ClientSession() as _cs:
                _cr_sol_price = await bc.get_sol_price(_cs)
                _cr_sol = bc.usd_to_sol(allocation["creator_credit"], _cr_sol_price)
                _cr_sig = await bc.send_sol_payout(_cs, CREATOR_WALLET, _cr_sol)
                # Debit jackpot reserve for the tx fee
                _fee_usd = bc.lamports_to_sol(bc.SOLANA_TX_FEE_LAMPORTS) * _cr_sol_price
                await db.update_jackpot_balance(-_fee_usd)
                await db.log_transaction(lobby_id, 0, "fee", _fee_usd, 0, 0,
                                         notes=f"Network fee for creator payout {lobby_id}")
            await db.log_transaction(lobby_id, 0, "creator_credit",
                                     allocation["creator_credit"], _cr_sol, _cr_sol_price,
                                     tx_signature=_cr_sig,
                                     notes=f"Creator payout from lobby {lobby_id}")
        except Exception as _cr_exc:
            logger.exception("Creator payout failed for lobby %s: %s", lobby_id, _cr_exc)
            # Still log the credit even if send fails
            await db.log_transaction(lobby_id, 0, "creator_credit",
                                     allocation["creator_credit"], 0, 0,
                                     notes=f"Creator allocation (send failed) {lobby_id}")
    if allocation["jackpot_debit"] > 0:
        await db.update_jackpot_balance(-allocation["jackpot_debit"])
        await db.log_transaction(lobby_id, 0, "jackpot_debit",
                                 allocation["jackpot_debit"], 0, 0,
                                 notes=f"Jackpot funded prize for lobby {lobby_id}")

    await db.update_lobby(
        lobby_id,
        prize_pool_usd=prize_pool,
        multiplier=entry.multiplier,
        status="playing",
    )

    # ── Spin animation ────────────────────────────────────
    frames = spin_logic.generate_spin_frames(prize_pool, buyin_usd, max_players)

    spin_msg = await bot.send_message(chat_id, frames[0])
    for frame in frames[1:]:
        await asyncio.sleep(0.5)
        try:
            await bot.edit_message_text(frame, chat_id=chat_id, message_id=spin_msg.message_id)
        except Exception:
            pass

    await asyncio.sleep(1.5)

    # ── Prize info ────────────────────────────────────────
    prize_text = f"🏆 <b>Prize Pool: ${prize_pool:,.2f}</b>\n"
    if max_players == 3:
        if len(payouts) == 1:
            prize_text += "Winner takes all!"
        else:
            for pos, amt in sorted(payouts.items()):
                prize_text += f"  {pos}{'st' if pos == 1 else 'nd'}: ${amt:,.2f}\n"
    else:
        for pos, amt in sorted(payouts.items()):
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(pos, "th")
            prize_text += f"  {pos}{suffix}: ${amt:,.2f}\n"

    await bot.send_message(chat_id, prize_text)
    await asyncio.sleep(1)

    # ── Create poker game ─────────────────────────────────
    players_data = await db.get_lobby_players(lobby_id)
    game_id = str(uuid.uuid4())[:12]

    player_states = []
    for i, pd in enumerate(players_data):
        player_states.append(PlayerState(
            telegram_id=pd["telegram_id"],
            username=pd["username"],
            seat=i,
        ))

    game = PokerGame(
        game_id=game_id,
        lobby_id=lobby_id,
        chat_id=chat_id,
        players=player_states,
        prize_pool_usd=prize_pool,
        buyin_usd=buyin_usd,
        payouts=payouts,
        blind_timer_start=time.time(),
    )

    # Deal first hand
    hand_info = game.start_new_hand()

    # Save to DB
    await db.create_game(game_id, lobby_id, chat_id, game.to_dict())
    active_games[chat_id] = game

    # Notify group
    sb_name = next(p.username for p in game.players if p.telegram_id == hand_info["sb_id"])
    bb_name = next(p.username for p in game.players if p.telegram_id == hand_info["bb_id"])
    await bot.send_message(
        chat_id,
        f"🃏 <b>Game started!</b>\n"
        f"SB: @{sb_name} ({hand_info['sb_amount']})\n"
        f"BB: @{bb_name} ({hand_info['bb_amount']})\n\n"
        "Hole cards have been sent via DM.",
    )

    # Send hole cards
    for p in game.alive_players:
        await _send_hole_cards_dm(p)

    # Post table
    table_msg = await bot.send_message(chat_id, game.render_table())
    game.message_id = table_msg.message_id
    await db.update_game(game_id, game_state=game.to_dict(), message_id=table_msg.message_id)

    # Start turn timer
    _start_turn_timer(game)


# ═══════════════════════════════════════════════════════════════════════════
# Poker action commands
# ═══════════════════════════════════════════════════════════════════════════

@router.message(Command("fold"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_fold(message: Message) -> None:
    await _handle_action(message, "fold")


@router.message(Command("check"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_check(message: Message) -> None:
    await _handle_action(message, "check")


@router.message(Command("call"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_call(message: Message) -> None:
    await _handle_action(message, "call")


@router.message(Command("bet"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_bet(message: Message) -> None:
    amount = _parse_amount(message.text)
    await _handle_action(message, "bet", amount)


@router.message(Command("raise"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_raise(message: Message) -> None:
    amount = _parse_amount(message.text)
    await _handle_action(message, "raise", amount)


@router.message(Command("allin"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_allin(message: Message) -> None:
    await _handle_action(message, "allin")


def _parse_amount(text: str | None) -> int:
    if not text:
        return 0
    parts = text.strip().split()
    if len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            pass
    return 0


async def _handle_action(message: Message, action: str, amount: int = 0) -> None:
    chat_id = message.chat.id
    uid = message.from_user.id

    game = active_games.get(chat_id)
    if not game:
        return  # No active game

    if game.phase in ("showdown", "finished"):
        return

    # Validate it's this player's turn
    cp = game.current_player
    if cp is None or cp.telegram_id != uid:
        await message.reply("⛔ It's not your turn.")
        return

    # Cancel turn timer
    _cancel_turn_timer(game.game_id)

    result = game.do_action(uid, action, amount)

    if not result["ok"]:
        await message.reply(f"❌ {result['msg']}")
        _start_turn_timer(game)
        return

    # Post action message
    await bot.send_message(chat_id, result["msg"])

    # Check hand over (one player left)
    if result["hand_over"]:
        await _resolve_hand(game, one_left=True)
        return

    # Advance phase if needed
    if result["advance_phase"]:
        # Check if all remaining are all-in
        if not game.active_non_allin:
            game.run_to_showdown()
            await _resolve_hand(game, one_left=False)
            return

        new_phase = game.advance_phase()
        if new_phase == "showdown":
            await _resolve_hand(game, one_left=False)
            return

        # Post new phase info
        phase_names = {"flop": "FLOP", "turn": "TURN", "river": "RIVER"}
        if new_phase in phase_names:
            await bot.send_message(
                chat_id,
                f"📋 <b>{phase_names[new_phase]}</b>: {card_display(game.community_cards)}",
            )

    # Update table message
    await _update_table_message(game)

    # Save state
    await db.update_game(game.game_id, game_state=game.to_dict())

    # Start new turn timer
    if game.phase not in ("showdown", "finished"):
        _start_turn_timer(game)


# ═══════════════════════════════════════════════════════════════════════════
# Hand resolution
# ═══════════════════════════════════════════════════════════════════════════

async def _resolve_hand(game: PokerGame, one_left: bool = False) -> None:
    """Resolve the current hand: showdown or last-player-standing."""
    chat_id = game.chat_id

    if one_left:
        results = game.determine_winners()
    else:
        results = game.determine_winners()
        showdown_text = game.render_showdown(results)
        await bot.send_message(chat_id, showdown_text)

    # Announce chip changes
    for r in results:
        if r["amount"] > 0:
            await bot.send_message(
                chat_id,
                f"💰 @{r['username']} wins {r['amount']} chips"
                + (f" ({r['hand_name']})" if r.get("hand_name") else ""),
            )

    # Eliminate busted players
    eliminated = game.eliminate_busted_players()
    for p in eliminated:
        await bot.send_message(chat_id, f"💀 @{p.username} has been eliminated!")

    # Check tournament over
    if game.is_tournament_over():
        await _finish_tournament(game)
        return

    # Check blind increase
    if game.check_and_increase_blinds():
        await bot.send_message(
            chat_id,
            f"⬆️ <b>Blinds increased!</b> Now {game.small_blind}/{game.big_blind}",
        )

    # Start new hand
    await asyncio.sleep(2)
    hand_info = game.start_new_hand()

    sb_name = next(p.username for p in game.players if p.telegram_id == hand_info["sb_id"])
    bb_name = next(p.username for p in game.players if p.telegram_id == hand_info["bb_id"])

    await bot.send_message(
        chat_id,
        f"🃏 <b>Hand #{game.hand_number}</b>\n"
        f"SB: @{sb_name} ({hand_info['sb_amount']})  |  "
        f"BB: @{bb_name} ({hand_info['bb_amount']})",
    )

    # DM hole cards
    for p in game.alive_players:
        await _send_hole_cards_dm(p)

    # Post new table
    table_msg = await bot.send_message(chat_id, game.render_table())
    game.message_id = table_msg.message_id

    await db.update_game(game.game_id, game_state=game.to_dict(), message_id=table_msg.message_id)
    _start_turn_timer(game)


# ═══════════════════════════════════════════════════════════════════════════
# Tournament finish & payouts
# ═══════════════════════════════════════════════════════════════════════════

async def _finish_tournament(game: PokerGame) -> None:
    chat_id = game.chat_id
    game.phase = "finished"

    standings = game.get_final_standings()

    lines = ["🏆 <b>TOURNAMENT COMPLETE!</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    for s in standings:
        pos = s["position"]
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(pos, f"{pos}.")
        payout = game.payouts.get(pos, 0)
        payout_str = f"  →  ${payout:,.2f}" if payout > 0 else ""
        lines.append(f"{medal} @{s['username']}{payout_str}")

    await bot.send_message(chat_id, "\n".join(lines))

    # Process payouts
    async with aiohttp.ClientSession() as session:
        sol_price = await bc.get_sol_price(session)

        for s in standings:
            pos = s["position"]
            usd_payout = game.payouts.get(pos, 0)
            if usd_payout <= 0:
                continue

            user = await db.get_user(s["telegram_id"])
            if not user or not user.get("wallet_address"):
                logger.error("No wallet for user %d — cannot pay.", s["telegram_id"])
                continue

            sol_amount = bc.usd_to_sol(usd_payout, sol_price)
            try:
                tx_sig = await bc.send_sol_payout(session, user["wallet_address"], sol_amount)
                # Debit jackpot reserve for the network fee of this payout
                fee_usd = bc.lamports_to_sol(bc.SOLANA_TX_FEE_LAMPORTS) * sol_price
                await db.update_jackpot_balance(-fee_usd)
                await db.log_transaction(
                    game.lobby_id, s["telegram_id"], "fee",
                    fee_usd, 0, sol_price,
                    notes=f"Network fee for payout pos {pos}",
                )
                await db.log_transaction(
                    game.lobby_id, s["telegram_id"], "payout",
                    usd_payout, sol_amount, sol_price,
                    tx_signature=tx_sig,
                    notes=f"Position {pos}",
                )
                await bot.send_message(
                    chat_id,
                    f"💸 Payout sent to @{s['username']}: "
                    f"{sol_amount:.6f} SOL (${usd_payout:,.2f})\n"
                    f"TX: <code>{tx_sig}</code>",
                )
            except Exception as exc:
                logger.exception("Payout failed for user %d: %s", s["telegram_id"], exc)
                await bot.send_message(
                    chat_id,
                    f"⚠️ Payout to @{s['username']} failed — admin will process manually.",
                )

    # Clean up
    active_games.pop(chat_id, None)
    _cancel_turn_timer(game.game_id)
    await db.update_game(game.game_id, status="finished", game_state=game.to_dict())
    await db.update_lobby(game.lobby_id, status="finished")


# ═══════════════════════════════════════════════════════════════════════════
# Table message updates
# ═══════════════════════════════════════════════════════════════════════════

async def _update_table_message(game: PokerGame) -> None:
    """Edit the live table message in the group."""
    try:
        text = game.render_table()
        if game.message_id:
            await bot.edit_message_text(
                text, chat_id=game.chat_id, message_id=game.message_id,
            )
    except Exception as exc:
        # Message might be too old or identical — send a new one
        try:
            msg = await bot.send_message(game.chat_id, game.render_table())
            game.message_id = msg.message_id
        except Exception:
            logger.warning("Cannot update table message: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# Turn timer (auto-fold on timeout)
# ═══════════════════════════════════════════════════════════════════════════

def _start_turn_timer(game: PokerGame) -> None:
    _cancel_turn_timer(game.game_id)
    task = asyncio.create_task(_turn_timeout(game))
    turn_timers[game.game_id] = task


def _cancel_turn_timer(game_id: str) -> None:
    task = turn_timers.pop(game_id, None)
    if task and not task.done():
        task.cancel()


async def _turn_timeout(game: PokerGame) -> None:
    """Auto-fold after TURN_TIMEOUT seconds of inactivity."""
    try:
        await asyncio.sleep(TURN_TIMEOUT)
    except asyncio.CancelledError:
        return

    cp = game.current_player
    if cp is None:
        return

    # Auto-check if possible, otherwise fold
    available = game.available_actions()
    action = "check" if "check" in available else "fold"

    await bot.send_message(
        game.chat_id,
        f"⏰ @{cp.username} timed out — auto {action}.",
    )

    result = game.do_action(cp.telegram_id, action)

    if result.get("hand_over"):
        await _resolve_hand(game, one_left=True)
        return

    if result.get("advance_phase"):
        if not game.active_non_allin:
            game.run_to_showdown()
            await _resolve_hand(game, one_left=False)
            return
        new_phase = game.advance_phase()
        if new_phase == "showdown":
            await _resolve_hand(game, one_left=False)
            return
        phase_names = {"flop": "FLOP", "turn": "TURN", "river": "RIVER"}
        if new_phase in phase_names:
            await bot.send_message(
                game.chat_id,
                f"📋 <b>{phase_names[new_phase]}</b>: {card_display(game.community_cards)}",
            )

    await _update_table_message(game)
    await db.update_game(game.game_id, game_state=game.to_dict())

    if game.phase not in ("showdown", "finished"):
        _start_turn_timer(game)


# ═══════════════════════════════════════════════════════════════════════════
# /status — show current game or lobby info (group)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(Command("status"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def cmd_status(message: Message) -> None:
    chat_id = message.chat.id
    game = active_games.get(chat_id)
    if game:
        await message.answer(game.render_table())
        return
    await message.answer("No active game in this group. Use /spin to start one.")


# ═══════════════════════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════════════════════

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "♠️ <b>SOL Poker — Spin & Gold</b>\n\n"
        "<b>Group commands:</b>\n"
        "/spin — Create a new game\n"
        "/fold /check /call — Poker actions\n"
        "/bet X /raise X — Bet or raise\n"
        "/allin — Go all in\n"
        "/status — Show current table\n\n"
        "<b>Private commands:</b>\n"
        "/start — Register / view wallet\n"
        "/editwallet — Change wallet\n\n"
        "🔗 <b>SOL on Solana only.</b> No USDC/USDT.\n"
        "No refunds."
    )
    await message.answer(text)


# ═══════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════

async def on_startup() -> None:
    await db.init_db()
    logger.info("Database initialised")

    # Restore active games from DB
    # (simplified — in production you'd do deeper state recovery)
    logger.info("Bot started successfully ✓")

    # Set bot commands
    await bot.set_my_commands([
        BotCommand(command="spin", description="Start a new Spin & Gold game"),
        BotCommand(command="fold", description="Fold your hand"),
        BotCommand(command="check", description="Check"),
        BotCommand(command="call", description="Call the current bet"),
        BotCommand(command="bet", description="Place a bet (e.g. /bet 50)"),
        BotCommand(command="raise", description="Raise (e.g. /raise 100)"),
        BotCommand(command="allin", description="Go all in"),
        BotCommand(command="status", description="Show current table"),
        BotCommand(command="help", description="Show help"),
    ])


async def on_shutdown() -> None:
    # Save all active games
    for chat_id, game in active_games.items():
        try:
            await db.update_game(game.game_id, game_state=game.to_dict())
        except Exception:
            pass
    await db.close_db()
    logger.info("Bot shutdown — state saved")


async def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    logger.info("Starting @SOLPoker_bot …")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
