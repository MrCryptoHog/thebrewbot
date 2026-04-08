"""
spin_logic.py — Spin & Gold prize-pool determination for SOLPoker Bot.

Implements:
 • GG-style multiplier tables for 3-max and 6-max
 • Dynamic probability adjustment based on jackpot reserve
 • Probability targeting layer (60-75 % sub-break-even, 20-30 % break-even, 5-10 % above)
 • Profit / reserve allocation after each spin
 • Spin animation text frames
"""

import logging
import math
import random
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("spin_logic")


# ═══════════════════════════════════════════════════════════
# Multiplier & Payout Structures
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MultiplierEntry:
    """One row of the spin table.

    *multiplier* is applied to a **single buy-in** to produce the prize pool.
    *base_weight* is the un-adjusted probability weight.
    *payouts* maps finishing position (1-based) → share of prize pool.
    *category* is one of ``'sub'``, ``'even'``, ``'above'``.
    """
    multiplier: float
    base_weight: float
    payouts: dict[int, float]
    category: str  # 'sub', 'even', 'above'


# ── 3-max tables ─────────────────────────────────────────
# Prize pool = multiplier × single buy-in
# Total collected = 3 × buy-in  →  break-even @ 3×

TABLE_3MAX: list[MultiplierEntry] = [
    MultiplierEntry(1.5,  4000, {1: 1.0},                         "sub"),
    MultiplierEntry(2.0,  3500, {1: 1.0},                         "sub"),
    MultiplierEntry(2.5,  1500, {1: 1.0},                         "sub"),
    MultiplierEntry(3.0,   600, {1: 1.0},                         "even"),
    MultiplierEntry(3.5,   200, {1: 1.0},                         "even"),
    MultiplierEntry(4.0,    90, {1: 0.80, 2: 0.20},               "above"),
    MultiplierEntry(5.0,    55, {1: 0.80, 2: 0.20},               "above"),
    MultiplierEntry(10.0,   30, {1: 0.75, 2: 0.25},               "above"),
    MultiplierEntry(25.0,   15, {1: 0.70, 2: 0.30},               "above"),
    MultiplierEntry(100.0,   8, {1: 0.65, 2: 0.35},               "above"),
    MultiplierEntry(500.0,   2, {1: 0.60, 2: 0.40},               "above"),
]

# ── 6-max tables ─────────────────────────────────────────
# Break-even @ 6×

TABLE_6MAX: list[MultiplierEntry] = [
    MultiplierEntry(2.0,  3500, {1: 1.0},                         "sub"),
    MultiplierEntry(3.0,  2800, {1: 1.0},                         "sub"),
    MultiplierEntry(4.0,  1800, {1: 0.70, 2: 0.30},               "sub"),
    MultiplierEntry(5.0,  1000, {1: 0.65, 2: 0.35},               "sub"),
    MultiplierEntry(6.0,   500, {1: 0.60, 2: 0.25, 3: 0.15},      "even"),
    MultiplierEntry(7.0,   200, {1: 0.55, 2: 0.27, 3: 0.18},      "even"),
    MultiplierEntry(10.0,   90, {1: 0.55, 2: 0.27, 3: 0.18},      "above"),
    MultiplierEntry(15.0,   50, {1: 0.55, 2: 0.27, 3: 0.18},      "above"),
    MultiplierEntry(25.0,   30, {1: 0.50, 2: 0.30, 3: 0.20},      "above"),
    MultiplierEntry(100.0,  15, {1: 0.50, 2: 0.30, 3: 0.20},      "above"),
    MultiplierEntry(500.0,   5, {1: 0.50, 2: 0.30, 3: 0.20},      "above"),
    MultiplierEntry(1000.0,  2, {1: 0.45, 2: 0.30, 3: 0.25},      "above"),
]


def _get_table(max_players: int) -> list[MultiplierEntry]:
    return TABLE_3MAX if max_players == 3 else TABLE_6MAX


# ═══════════════════════════════════════════════════════════
# Dynamic probability engine
# ═══════════════════════════════════════════════════════════

def compute_probabilities(
    max_players: int,
    buyin_usd: float,
    jackpot_reserve: float,
) -> list[tuple[MultiplierEntry, float]]:
    """
    Return ``[(entry, probability), …]`` dynamically adjusted so that:
     • The system never pays out more than collected + reserve.
     • Category targets (sub / even / above) are respected.
     • Reserve level shifts weight toward safety when low.
    """
    table = _get_table(max_players)
    total_collected = buyin_usd * max_players

    # ── 1. Compute adjustment factor from reserve health ──
    # A "healthy" reserve can support ~5 average jackpot payouts.
    avg_above_deficit = _avg_above_deficit(table, buyin_usd, max_players)
    if avg_above_deficit > 0:
        reserve_ratio = jackpot_reserve / (avg_above_deficit * 5 + 0.01)
    else:
        reserve_ratio = 10.0  # no above entries → very healthy

    # Clamp 0 .. 3
    reserve_ratio = max(0.0, min(reserve_ratio, 3.0))

    # ── 2. Adjust weights ──
    adjusted: list[tuple[MultiplierEntry, float]] = []
    for entry in table:
        w = entry.base_weight

        prize = entry.multiplier * buyin_usd
        deficit = prize - total_collected

        if entry.category == "sub":
            # Boost when reserve is low
            w *= 1.0 + (1.0 - min(reserve_ratio, 1.0)) * 0.5
        elif entry.category == "even":
            pass  # keep base
        elif entry.category == "above":
            if deficit > jackpot_reserve:
                # Cannot afford this tier — zero it out
                w = 0.0
            else:
                # Scale down when reserve is low, up when high
                w *= min(reserve_ratio, 2.0) / 2.0
                # Apply soft clustering prevention (small random jitter)
                w *= random.uniform(0.85, 1.15)

        adjusted.append((entry, w))

    # ── 3. Normalise to probability ──
    total_w = sum(w for _, w in adjusted)
    if total_w == 0:
        # Extreme edge case: return guaranteed lowest tier
        return [(table[0], 1.0)]

    probs = [(e, w / total_w) for e, w in adjusted]

    # ── 4. Enforce category targets ──
    probs = _enforce_category_targets(probs)

    return probs


def _avg_above_deficit(
    table: list[MultiplierEntry],
    buyin_usd: float,
    max_players: int,
) -> float:
    """Average deficit of 'above' entries (how much jackpot they'd need)."""
    total_collected = buyin_usd * max_players
    deficits = []
    for e in table:
        if e.category == "above":
            prize = e.multiplier * buyin_usd
            deficits.append(max(0, prize - total_collected))
    return sum(deficits) / len(deficits) if deficits else 0.0


def _enforce_category_targets(
    probs: list[tuple[MultiplierEntry, float]],
) -> list[tuple[MultiplierEntry, float]]:
    """
    Re-scale probabilities so aggregated categories land within:
      sub:   60-75 %
      even:  20-30 %
      above:  5-10 %
    """
    cat_sums: dict[str, float] = {"sub": 0, "even": 0, "above": 0}
    for e, p in probs:
        cat_sums[e.category] += p

    targets = {"sub": (0.60, 0.75), "even": (0.20, 0.30), "above": (0.05, 0.10)}

    # Compute a simple scaling factor per category
    scales: dict[str, float] = {}
    for cat, (lo, hi) in targets.items():
        current = cat_sums[cat]
        if current <= 0:
            scales[cat] = 1.0
            continue
        mid = (lo + hi) / 2
        scales[cat] = mid / current

    # Apply scales
    new_probs = [(e, p * scales[e.category]) for e, p in probs]

    # Re-normalise
    total = sum(p for _, p in new_probs)
    if total == 0:
        return [(probs[0][0], 1.0)]
    return [(e, p / total) for e, p in new_probs]


# ═══════════════════════════════════════════════════════════
# Spin execution
# ═══════════════════════════════════════════════════════════

def spin(
    max_players: int,
    buyin_usd: float,
    jackpot_reserve: float,
) -> MultiplierEntry:
    """Choose a multiplier entry using the dynamic probability engine."""
    probs = compute_probabilities(max_players, buyin_usd, jackpot_reserve)
    entries, weights = zip(*probs)
    chosen = random.choices(entries, weights=weights, k=1)[0]
    logger.info(
        "Spin result: %sx (category=%s) for %d-max $%.2f buy-in  reserve=$%.2f",
        chosen.multiplier, chosen.category, max_players, buyin_usd, jackpot_reserve,
    )
    return chosen


def calculate_prize_pool(entry: MultiplierEntry, buyin_usd: float) -> float:
    """Dollar prize pool for a given multiplier and buy-in."""
    return round(entry.multiplier * buyin_usd, 2)


def get_payout_amounts(
    entry: MultiplierEntry,
    buyin_usd: float,
) -> dict[int, float]:
    """Map finishing position → USD payout."""
    pool = calculate_prize_pool(entry, buyin_usd)
    return {pos: round(pool * share, 2) for pos, share in entry.payouts.items()}


# ═══════════════════════════════════════════════════════════
# Post-spin allocation
# ═══════════════════════════════════════════════════════════

def compute_allocation(
    entry: MultiplierEntry,
    buyin_usd: float,
    max_players: int,
) -> dict:
    """
    Return:
      total_collected, prize_pool, surplus, deficit,
      jackpot_credit, creator_credit, jackpot_debit
    """
    total_collected = buyin_usd * max_players
    prize_pool = calculate_prize_pool(entry, buyin_usd)

    result = {
        "total_collected": total_collected,
        "prize_pool": prize_pool,
        "surplus": 0.0,
        "deficit": 0.0,
        "jackpot_credit": 0.0,
        "creator_credit": 0.0,
        "jackpot_debit": 0.0,
    }

    if prize_pool <= total_collected:
        surplus = total_collected - prize_pool
        result["surplus"] = surplus
        result["jackpot_credit"] = round(surplus * 0.70, 2)
        result["creator_credit"] = round(surplus * 0.30, 2)
    else:
        deficit = prize_pool - total_collected
        result["deficit"] = deficit
        result["jackpot_debit"] = deficit

    return result


# ═══════════════════════════════════════════════════════════
# Spin animation frames
# ═══════════════════════════════════════════════════════════

def generate_spin_frames(
    final_prize_usd: float,
    buyin_usd: float,
    max_players: int,
    num_frames: int = 11,
) -> list[str]:
    """
    Create *num_frames* text frames for the spin animation.
    Each frame shows a random dollar amount from the table;
    the last frame shows the real prize.
    """
    table = _get_table(max_players)
    possible_prizes = [round(e.multiplier * buyin_usd, 2) for e in table]

    frames: list[str] = []
    for i in range(num_frames - 1):
        fake = random.choice(possible_prizes)
        frames.append(_spin_frame(fake, spinning=True))

    # Final frame
    frames.append(_spin_frame(final_prize_usd, spinning=False))
    return frames


def _spin_frame(amount: float, spinning: bool) -> str:
    if spinning:
        bar = "▓" * random.randint(3, 8) + "░" * random.randint(2, 5)
        return (
            f"🎰  SPINNING…\n"
            f"━━━━━━━━━━━━━━\n"
            f"  {bar}\n"
            f"  💰 ${amount:,.2f}\n"
            f"━━━━━━━━━━━━━━"
        )
    else:
        return (
            f"🎰  PRIZE LOCKED! 🔒\n"
            f"━━━━━━━━━━━━━━\n"
            f"  🏆  💰 ${amount:,.2f}  🏆\n"
            f"━━━━━━━━━━━━━━"
        )
