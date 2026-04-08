"""
blockchain.py — Solana RPC integration for SOLPoker Bot.
Handles: SOL price fetching, deposit verification, SOL transfers (payouts).
All amounts on-chain are in lamports (1 SOL = 1_000_000_000 lamports).
"""

import asyncio
import base64
import logging
import os
import time
from typing import Optional

import aiohttp
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.system_program import transfer, TransferParams  # type: ignore
from solders.transaction import Transaction  # type: ignore
from solders.message import Message  # type: ignore
from solders.hash import Hash  # type: ignore

logger = logging.getLogger("blockchain")

LAMPORTS_PER_SOL = 1_000_000_000
SOLANA_TX_FEE_LAMPORTS = 5_000  # standard Solana base tx fee

# ── Env ──────────────────────────────────────────────────────
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MAIN_WALLET_ADDRESS = os.getenv("MAIN_WALLET_ADDRESS", "")
HOT_WALLET_PRIVATE_KEY = os.getenv("HOT_WALLET_PRIVATE_KEY", "")

# ── Price cache ──────────────────────────────────────────────
_price_cache: dict[str, tuple[float, float]] = {}  # {"sol": (timestamp, price)}
PRICE_CACHE_TTL = 60  # seconds


# ═══════════════════════════════════════════════════════════
# SOL price
# ═══════════════════════════════════════════════════════════

async def get_sol_price(session: Optional[aiohttp.ClientSession] = None) -> float:
    """Fetch real-time SOL/USD price. Tries multiple sources, caches result."""
    cached = _price_cache.get("sol")
    if cached and time.time() - cached[0] < PRICE_CACHE_TTL:
        return cached[1]

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()

    try:
        price = await _try_jupiter(session)
        if price and 10 < price < 2000:
            _price_cache["sol"] = (time.time(), price)
            return price

        price = await _try_coingecko(session)
        if price and 10 < price < 2000:
            _price_cache["sol"] = (time.time(), price)
            return price

        price = await _try_binance(session)
        if price and 10 < price < 2000:
            _price_cache["sol"] = (time.time(), price)
            return price

        # Fallback to cached even if stale
        if cached:
            logger.warning("All price sources failed – returning stale cache")
            return cached[1]

        raise RuntimeError("Unable to fetch SOL price from any source")
    finally:
        if own_session and session:
            await session.close()


async def _try_jupiter(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            data = await resp.json()
            return float(data["data"]["So11111111111111111111111111111111111111112"]["price"])
    except Exception as exc:
        logger.debug("Jupiter price failed: %s", exc)
        return None


async def _try_coingecko(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            data = await resp.json()
            return float(data["solana"]["usd"])
    except Exception as exc:
        logger.debug("CoinGecko price failed: %s", exc)
        return None


async def _try_binance(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            data = await resp.json()
            return float(data["price"])
    except Exception as exc:
        logger.debug("Binance price failed: %s", exc)
        return None


def sol_to_lamports(sol: float) -> int:
    return int(round(sol * LAMPORTS_PER_SOL))


def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


def usd_to_sol(usd: float, sol_price: float) -> float:
    """Convert USD amount to SOL, rounded to 6 decimals."""
    return round(usd / sol_price, 6)


# ═══════════════════════════════════════════════════════════
# Deposit verification
# ═══════════════════════════════════════════════════════════

async def get_recent_signatures(
    session: aiohttp.ClientSession,
    address: str,
    limit: int = 30,
) -> list[dict]:
    """Fetch recent confirmed transaction signatures for *address*."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit, "commitment": "confirmed"}],
    }
    async with session.post(SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
    return data.get("result", [])


async def get_transaction_detail(
    session: aiohttp.ClientSession,
    signature: str,
) -> Optional[dict]:
    """Return parsed transaction data for a given signature."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                               "maxSupportedTransactionVersion": 0}],
    }
    try:
        async with session.post(SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
        return data.get("result")
    except Exception as exc:
        logger.error("getTransaction failed for %s: %s", signature, exc)
        return None


def parse_sol_transfer(tx_data: dict, main_wallet: str) -> Optional[dict]:
    """
    Parse a transaction and extract sender wallet + SOL amount received
    by *main_wallet*. Returns ``{sender, lamports, sol}`` or ``None``.
    """
    try:
        meta = tx_data.get("meta")
        if meta is None or meta.get("err") is not None:
            return None

        msg = tx_data["transaction"]["message"]
        account_keys = []
        # Handle both legacy and versioned formats
        if isinstance(msg.get("accountKeys"), list):
            for k in msg["accountKeys"]:
                if isinstance(k, str):
                    account_keys.append(k)
                elif isinstance(k, dict):
                    account_keys.append(k.get("pubkey", ""))
                else:
                    account_keys.append(str(k))

        if main_wallet not in account_keys:
            return None

        idx = account_keys.index(main_wallet)
        pre = meta["preBalances"][idx]
        post = meta["postBalances"][idx]
        received_lamports = post - pre

        if received_lamports <= 0:
            return None

        # Identify sender: other account whose balance decreased the most
        best_sender = None
        best_decrease = 0
        for i, addr in enumerate(account_keys):
            if addr == main_wallet:
                continue
            decrease = meta["preBalances"][i] - meta["postBalances"][i]
            if decrease > best_decrease:
                best_decrease = decrease
                best_sender = addr

        if best_sender is None:
            return None

        return {
            "sender": best_sender,
            "lamports": received_lamports,
            "sol": lamports_to_sol(received_lamports),
        }
    except Exception as exc:
        logger.error("parse_sol_transfer error: %s", exc)
        return None


async def check_deposits(
    session: aiohttp.ClientSession,
    pending_players: list[dict],
    required_sol: float,
    tolerance_sol: float = 0.0005,
) -> list[dict]:
    """
    Scan recent transactions to the main wallet and match against
    *pending_players* (list of dicts with ``telegram_id``, ``wallet_address``).
    Returns list of confirmed player dicts with ``tx_signature``.
    """
    from database import is_signature_processed, mark_signature_processed

    confirmed: list[dict] = []
    sigs = await get_recent_signatures(session, MAIN_WALLET_ADDRESS)

    wallet_to_player = {p["wallet_address"]: p for p in pending_players}
    required_lamports = sol_to_lamports(required_sol)
    tolerance_lamports = sol_to_lamports(tolerance_sol)

    for sig_info in sigs:
        sig = sig_info["signature"]
        if await is_signature_processed(sig):
            continue

        tx = await get_transaction_detail(session, sig)
        if tx is None:
            continue

        parsed = parse_sol_transfer(tx, MAIN_WALLET_ADDRESS)
        if parsed is None:
            continue

        sender = parsed["sender"]
        if sender not in wallet_to_player:
            # Mark processed so we don't re-check
            await mark_signature_processed(sig)
            continue

        # Amount check: must be >= required (minus small tolerance)
        if parsed["lamports"] >= (required_lamports - tolerance_lamports):
            player = wallet_to_player[sender]
            player["tx_signature"] = sig
            player["received_sol"] = parsed["sol"]
            player["received_lamports"] = parsed["lamports"]
            confirmed.append(player)
            await mark_signature_processed(sig)
            # Remove from lookup to avoid double-matching
            del wallet_to_player[sender]

    return confirmed


# ═══════════════════════════════════════════════════════════
# SOL payouts
# ═══════════════════════════════════════════════════════════

def _get_hot_wallet_keypair() -> Keypair:
    """Load the hot-wallet keypair from env."""
    return Keypair.from_base58_string(HOT_WALLET_PRIVATE_KEY)


async def send_sol(
    session: aiohttp.ClientSession,
    recipient_address: str,
    lamports: int,
) -> str:
    """
    Transfer *lamports* of SOL from the hot wallet to *recipient_address*.
    Returns the transaction signature string.
    """
    sender_kp = _get_hot_wallet_keypair()
    recipient = Pubkey.from_string(recipient_address)

    # 1. Get latest blockhash
    bh_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getLatestBlockhash",
        "params": [{"commitment": "finalized"}],
    }
    async with session.post(SOLANA_RPC_URL, json=bh_payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        bh_data = await resp.json()

    blockhash_str = bh_data["result"]["value"]["blockhash"]
    blockhash = Hash.from_string(blockhash_str)

    # 2. Build transfer instruction
    ix = transfer(TransferParams(
        from_pubkey=sender_kp.pubkey(),
        to_pubkey=recipient,
        lamports=lamports,
    ))

    # 3. Build, sign, serialise transaction
    msg = Message.new_with_blockhash([ix], sender_kp.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([sender_kp], blockhash)
    tx_bytes = bytes(tx)
    tx_b64 = base64.b64encode(tx_bytes).decode()

    # 4. Submit
    send_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ],
    }
    async with session.post(SOLANA_RPC_URL, json=send_payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        send_data = await resp.json()

    if "error" in send_data:
        raise RuntimeError(f"sendTransaction error: {send_data['error']}")

    signature = send_data["result"]
    logger.info("SOL sent: %s lamports → %s  sig=%s", lamports, recipient_address, signature)
    return signature


async def send_sol_payout(
    session: aiohttp.ClientSession,
    recipient_address: str,
    amount_sol: float,
) -> str:
    """High-level payout helper. Converts SOL to lamports then sends."""
    lamports = sol_to_lamports(amount_sol)
    # Reserve a small buffer for tx fee (5000 lamports)
    if lamports <= 5000:
        raise ValueError("Payout amount too small to cover fees")
    return await send_sol(session, recipient_address, lamports)


async def get_wallet_balance(session: aiohttp.ClientSession, address: Optional[str] = None) -> float:
    """Get SOL balance of *address* (defaults to main wallet). Returns SOL."""
    address = address or MAIN_WALLET_ADDRESS
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address, {"commitment": "confirmed"}],
    }
    async with session.post(SOLANA_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    lamports = data.get("result", {}).get("value", 0)
    return lamports_to_sol(lamports)
