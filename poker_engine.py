"""
poker_engine.py — Full Texas Hold'em engine for SOLPoker Bot.

Features
--------
 • Standard 52-card deck with proper shuffling
 • 7-card → best-5-card hand evaluation (all 21 combos)
 • Hyper-turbo Spin & Gold blind structure
 • Side-pot calculation for multi-way all-ins
 • Full betting-round state machine
 • Serialisable game state (JSON-safe dicts)
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from itertools import combinations
from typing import Optional

logger = logging.getLogger("poker_engine")

# ═══════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════

RANKS = list(range(2, 15))  # 2..14  (14 = Ace)
SUITS = list(range(4))       # 0=♠  1=♥  2=♦  3=♣

RANK_NAMES = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8",
    9: "9", 10: "T", 11: "J", 12: "Q", 13: "K", 14: "A",
}
SUIT_SYMBOLS = {0: "♠", 1: "♥", 2: "♦", 3: "♣"}

STARTING_CHIPS = 500

BLIND_LEVELS = [
    (10, 20),
    (15, 30),
    (20, 40),
    (25, 50),
    (50, 100),
    (75, 150),
    (100, 200),
    (150, 300),
    (200, 400),
    (300, 600),
    (400, 800),
    (500, 1000),
]
BLIND_INTERVAL_SECONDS = 180  # 3 minutes per level

HAND_RANK_NAMES = {
    0: "High Card",
    1: "One Pair",
    2: "Two Pair",
    3: "Three of a Kind",
    4: "Straight",
    5: "Flush",
    6: "Full House",
    7: "Four of a Kind",
    8: "Straight Flush",
}


# ═══════════════════════════════════════════════════════════
# Card helpers
# ═══════════════════════════════════════════════════════════

def card_str(rank: int, suit: int) -> str:
    return f"{RANK_NAMES[rank]}{SUIT_SYMBOLS[suit]}"


def card_str_from_tuple(c: tuple[int, int]) -> str:
    return card_str(c[0], c[1])


def card_display(cards: list[tuple[int, int]]) -> str:
    return "  ".join(card_str_from_tuple(c) for c in cards)


def make_deck() -> list[tuple[int, int]]:
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


# ═══════════════════════════════════════════════════════════
# Hand evaluation
# ═══════════════════════════════════════════════════════════

def evaluate_five(cards: list[tuple[int, int]]) -> tuple:
    """
    Score a 5-card hand.  Returns a comparable tuple:
      (hand_rank, *kickers)
    Higher is better.
    """
    ranks = sorted([c[0] for c in cards], reverse=True)
    suits = [c[1] for c in cards]

    is_flush = len(set(suits)) == 1

    # Straight detection
    is_straight = False
    high = ranks[0]
    if ranks == list(range(ranks[0], ranks[0] - 5, -1)):
        is_straight = True
    elif set(ranks) == {14, 5, 4, 3, 2}:
        is_straight = True
        high = 5  # wheel

    freq = Counter(ranks)
    counts = sorted(freq.values(), reverse=True)
    # Sort groups by (count desc, rank desc)
    groups = sorted(freq.items(), key=lambda x: (x[1], x[0]), reverse=True)

    if is_straight and is_flush:
        return (8, high)
    if counts == [4, 1]:
        return (7, groups[0][0], groups[1][0])
    if counts == [3, 2]:
        return (6, groups[0][0], groups[1][0])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, high)
    if counts == [3, 1, 1]:
        kickers = sorted([r for r, c in freq.items() if c == 1], reverse=True)
        return (3, groups[0][0], *kickers)
    if counts == [2, 2, 1]:
        pairs = sorted([r for r, c in freq.items() if c == 2], reverse=True)
        kicker = [r for r, c in freq.items() if c == 1][0]
        return (2, *pairs, kicker)
    if counts == [2, 1, 1, 1]:
        pair_rank = [r for r, c in freq.items() if c == 2][0]
        kickers = sorted([r for r, c in freq.items() if c == 1], reverse=True)
        return (1, pair_rank, *kickers)
    return (0, *ranks)


def best_hand(cards: list[tuple[int, int]]) -> tuple:
    """Evaluate best 5-card hand from 5-7 cards."""
    if len(cards) < 5:
        raise ValueError("Need at least 5 cards")
    best = None
    for combo in combinations(cards, 5):
        score = evaluate_five(list(combo))
        if best is None or score > best:
            best = score
    return best  # type: ignore


def hand_rank_name(score: tuple) -> str:
    return HAND_RANK_NAMES.get(score[0], "Unknown")


def compare_hands(
    players_cards: dict[int, list[tuple[int, int]]],
    community: list[tuple[int, int]],
) -> list[list[int]]:
    """
    Rank players by hand strength.
    Returns list of tiers: [[best_player_ids], [next_best], …].
    Ties share the same tier.
    """
    scores: dict[int, tuple] = {}
    for pid, hole in players_cards.items():
        all_cards = hole + community
        scores[pid] = best_hand(all_cards)

    # Sort descending
    sorted_players = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    tiers: list[list[int]] = []
    prev_score = None
    for pid, sc in sorted_players:
        if sc == prev_score and tiers:
            tiers[-1].append(pid)
        else:
            tiers.append([pid])
        prev_score = sc

    return tiers


# ═══════════════════════════════════════════════════════════
# Side-pot calculation
# ═══════════════════════════════════════════════════════════

def calculate_pots(
    player_contributions: dict[int, int],
    folded_ids: set[int],
) -> list[tuple[int, list[int]]]:
    """
    Build main pot + side pots.

    Parameters
    ----------
    player_contributions : {player_id: total_chips_put_in_this_hand}
    folded_ids : set of player_ids who folded

    Returns
    -------
    List of (pot_amount, [eligible_player_ids])
    *eligible* means still in the hand (not folded) AND contributed enough.
    """
    contribs = [
        {"id": pid, "bet": amt, "folded": pid in folded_ids}
        for pid, amt in player_contributions.items()
    ]
    contribs.sort(key=lambda x: x["bet"])

    pots: list[tuple[int, list[int]]] = []
    while contribs:
        min_bet = contribs[0]["bet"]
        if min_bet == 0:
            contribs = [c for c in contribs if c["bet"] > 0]
            if not contribs:
                break
            min_bet = contribs[0]["bet"]

        pot_amount = 0
        eligible: list[int] = []

        for c in contribs:
            contrib = min(c["bet"], min_bet)
            pot_amount += contrib
            c["bet"] -= contrib
            if not c["folded"]:
                eligible.append(c["id"])

        if pot_amount > 0 and eligible:
            pots.append((pot_amount, eligible))

        contribs = [c for c in contribs if c["bet"] > 0]

    return pots


# ═══════════════════════════════════════════════════════════
# Game-state dataclass (JSON-serialisable)
# ═══════════════════════════════════════════════════════════

@dataclass
class PlayerState:
    telegram_id: int
    username: str
    chips: int = STARTING_CHIPS
    hole_cards: list[tuple[int, int]] = field(default_factory=list)
    is_folded: bool = False
    is_all_in: bool = False
    is_eliminated: bool = False
    current_bet: int = 0
    total_bet_in_hand: int = 0
    seat: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hole_cards"] = [list(c) for c in self.hole_cards]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerState":
        d = dict(d)
        d["hole_cards"] = [tuple(c) for c in d.get("hole_cards", [])]
        return cls(**d)


@dataclass
class PokerGame:
    """Full mutable state for one poker hand / tournament."""
    game_id: str
    lobby_id: str
    chat_id: int
    players: list[PlayerState]

    deck: list[tuple[int, int]] = field(default_factory=list)
    community_cards: list[tuple[int, int]] = field(default_factory=list)

    phase: str = "preflop"   # preflop | flop | turn | river | showdown | finished
    pot: int = 0

    dealer_idx: int = 0
    current_player_idx: int = 0
    last_raiser_idx: Optional[int] = None

    small_blind: int = 10
    big_blind: int = 20
    blind_level: int = 0
    blind_timer_start: float = 0.0

    min_raise: int = 0

    # Prize structure
    prize_pool_usd: float = 0.0
    buyin_usd: float = 0.0
    payouts: dict[int, float] = field(default_factory=dict)  # position → USD

    # Turn tracking
    actions_this_round: int = 0
    players_acted_this_round: set[int] = field(default_factory=set)

    message_id: Optional[int] = None
    hand_number: int = 0

    # ── Serialisation ──────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "lobby_id": self.lobby_id,
            "chat_id": self.chat_id,
            "players": [p.to_dict() for p in self.players],
            "deck": [list(c) for c in self.deck],
            "community_cards": [list(c) for c in self.community_cards],
            "phase": self.phase,
            "pot": self.pot,
            "dealer_idx": self.dealer_idx,
            "current_player_idx": self.current_player_idx,
            "last_raiser_idx": self.last_raiser_idx,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "blind_level": self.blind_level,
            "blind_timer_start": self.blind_timer_start,
            "min_raise": self.min_raise,
            "prize_pool_usd": self.prize_pool_usd,
            "buyin_usd": self.buyin_usd,
            "payouts": {str(k): v for k, v in self.payouts.items()},
            "actions_this_round": self.actions_this_round,
            "players_acted_this_round": list(self.players_acted_this_round),
            "message_id": self.message_id,
            "hand_number": self.hand_number,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PokerGame":
        players = [PlayerState.from_dict(p) for p in d["players"]]
        payouts = {int(k): v for k, v in d.get("payouts", {}).items()}
        game = cls(
            game_id=d["game_id"],
            lobby_id=d["lobby_id"],
            chat_id=d["chat_id"],
            players=players,
            deck=[tuple(c) for c in d.get("deck", [])],
            community_cards=[tuple(c) for c in d.get("community_cards", [])],
            phase=d.get("phase", "preflop"),
            pot=d.get("pot", 0),
            dealer_idx=d.get("dealer_idx", 0),
            current_player_idx=d.get("current_player_idx", 0),
            last_raiser_idx=d.get("last_raiser_idx"),
            small_blind=d.get("small_blind", 10),
            big_blind=d.get("big_blind", 20),
            blind_level=d.get("blind_level", 0),
            blind_timer_start=d.get("blind_timer_start", 0.0),
            min_raise=d.get("min_raise", 0),
            prize_pool_usd=d.get("prize_pool_usd", 0.0),
            buyin_usd=d.get("buyin_usd", 0.0),
            payouts=payouts,
            actions_this_round=d.get("actions_this_round", 0),
            players_acted_this_round=set(d.get("players_acted_this_round", [])),
            message_id=d.get("message_id"),
            hand_number=d.get("hand_number", 0),
        )
        return game

    # ── Active-player helpers ──────────────────────────────

    @property
    def active_players(self) -> list[PlayerState]:
        """Players still in the hand (not folded, not eliminated)."""
        return [p for p in self.players if not p.is_folded and not p.is_eliminated]

    @property
    def active_non_allin(self) -> list[PlayerState]:
        """Active players who can still act (not all-in)."""
        return [p for p in self.active_players if not p.is_all_in]

    @property
    def alive_players(self) -> list[PlayerState]:
        """Players not yet eliminated from the tournament."""
        return [p for p in self.players if not p.is_eliminated]

    @property
    def current_player(self) -> Optional[PlayerState]:
        if 0 <= self.current_player_idx < len(self.players):
            return self.players[self.current_player_idx]
        return None

    def _next_active_seat(self, from_idx: int) -> Optional[int]:
        """Return the index of the next active non-allin player after *from_idx*."""
        n = len(self.players)
        for offset in range(1, n + 1):
            idx = (from_idx + offset) % n
            p = self.players[idx]
            if not p.is_folded and not p.is_eliminated and not p.is_all_in:
                return idx
        return None

    def _next_alive_seat(self, from_idx: int) -> int:
        n = len(self.players)
        for offset in range(1, n + 1):
            idx = (from_idx + offset) % n
            if not self.players[idx].is_eliminated:
                return idx
        return from_idx

    # ── Blind timer ────────────────────────────────────────

    def check_and_increase_blinds(self) -> bool:
        """Increase blinds if enough time has elapsed. Returns True if changed."""
        if self.blind_timer_start <= 0:
            return False
        elapsed = time.time() - self.blind_timer_start
        new_level = min(int(elapsed // BLIND_INTERVAL_SECONDS), len(BLIND_LEVELS) - 1)
        if new_level > self.blind_level:
            self.blind_level = new_level
            self.small_blind, self.big_blind = BLIND_LEVELS[new_level]
            logger.info("Blinds increased to %d/%d (level %d)", self.small_blind, self.big_blind, new_level)
            return True
        return False

    # ── New hand ───────────────────────────────────────────

    def start_new_hand(self) -> dict:
        """
        Reset for a new hand. Returns info dict with ``{sb_id, bb_id}``.
        """
        self.hand_number += 1
        self.check_and_increase_blinds()

        # Reset player hand state
        for p in self.players:
            p.hole_cards = []
            p.is_folded = p.is_eliminated
            p.is_all_in = False
            p.current_bet = 0
            p.total_bet_in_hand = 0

        self.deck = make_deck()
        self.community_cards = []
        self.pot = 0
        self.phase = "preflop"
        self.last_raiser_idx = None
        self.actions_this_round = 0
        self.players_acted_this_round = set()

        # Advance dealer
        if self.hand_number > 1:
            self.dealer_idx = self._next_alive_seat(self.dealer_idx)

        alive = self.alive_players
        n_alive = len(alive)

        if n_alive == 2:
            # Heads-up: dealer = SB
            sb_idx = self.dealer_idx
            bb_idx = self._next_alive_seat(sb_idx)
        else:
            sb_idx = self._next_alive_seat(self.dealer_idx)
            bb_idx = self._next_alive_seat(sb_idx)

        # Post blinds
        sb_player = self.players[sb_idx]
        bb_player = self.players[bb_idx]

        sb_amt = min(self.small_blind, sb_player.chips)
        bb_amt = min(self.big_blind, bb_player.chips)

        sb_player.chips -= sb_amt
        sb_player.current_bet = sb_amt
        sb_player.total_bet_in_hand = sb_amt
        if sb_player.chips == 0:
            sb_player.is_all_in = True

        bb_player.chips -= bb_amt
        bb_player.current_bet = bb_amt
        bb_player.total_bet_in_hand = bb_amt
        if bb_player.chips == 0:
            bb_player.is_all_in = True

        self.pot = sb_amt + bb_amt
        self.min_raise = self.big_blind

        # Deal hole cards
        for p in self.alive_players:
            p.hole_cards = [self.deck.pop(), self.deck.pop()]

        # First to act preflop: left of BB
        first = self._next_active_seat(bb_idx)
        if first is None:
            first = bb_idx  # everyone all-in
        self.current_player_idx = first

        return {
            "sb_id": sb_player.telegram_id,
            "bb_id": bb_player.telegram_id,
            "sb_amount": sb_amt,
            "bb_amount": bb_amt,
        }

    # ── Betting actions ────────────────────────────────────

    def _current_max_bet(self) -> int:
        return max((p.current_bet for p in self.active_players), default=0)

    def available_actions(self) -> list[str]:
        """Return list of valid action names for the current player."""
        p = self.current_player
        if p is None or p.is_folded or p.is_eliminated or p.is_all_in:
            return []

        actions = ["fold"]
        to_call = self._current_max_bet() - p.current_bet

        if to_call == 0:
            actions.append("check")
        else:
            actions.append("call")

        # Bet / Raise
        if to_call == 0:
            if p.chips >= self.big_blind:
                actions.append("bet")
        else:
            min_raise_total = self._current_max_bet() + self.min_raise
            if p.chips + p.current_bet > self._current_max_bet():
                actions.append("raise")

        actions.append("allin")
        return actions

    def do_action(self, player_id: int, action: str, amount: int = 0) -> dict:
        """
        Process a player action.

        Returns a dict:
          ok: bool
          msg: str
          advance_phase: bool  — True if the betting round is complete
          hand_over: bool — True if the hand ended (one player left)
        """
        result = {"ok": False, "msg": "", "advance_phase": False, "hand_over": False}

        p = self.current_player
        if p is None or p.telegram_id != player_id:
            result["msg"] = "It's not your turn."
            return result

        action = action.lower().strip()
        valid = self.available_actions()

        if action == "fold":
            p.is_folded = True
            result["ok"] = True
            result["msg"] = f"{p.username} folds."

        elif action == "check":
            if "check" not in valid:
                result["msg"] = "You cannot check — there's a bet to you."
                return result
            result["ok"] = True
            result["msg"] = f"{p.username} checks."

        elif action == "call":
            if "call" not in valid:
                result["msg"] = "Nothing to call."
                return result
            to_call = self._current_max_bet() - p.current_bet
            actual = min(to_call, p.chips)
            p.chips -= actual
            p.current_bet += actual
            p.total_bet_in_hand += actual
            self.pot += actual
            if p.chips == 0:
                p.is_all_in = True
            result["ok"] = True
            result["msg"] = f"{p.username} calls {actual}."

        elif action == "bet":
            if "bet" not in valid:
                result["msg"] = "You cannot bet here."
                return result
            if amount < self.big_blind:
                amount = self.big_blind
            amount = min(amount, p.chips)
            p.chips -= amount
            p.current_bet += amount
            p.total_bet_in_hand += amount
            self.pot += amount
            self.min_raise = amount
            self.last_raiser_idx = self.current_player_idx
            self.players_acted_this_round = {p.telegram_id}
            if p.chips == 0:
                p.is_all_in = True
            result["ok"] = True
            result["msg"] = f"{p.username} bets {amount}."

        elif action == "raise":
            if "raise" not in valid:
                result["msg"] = "You cannot raise here."
                return result
            to_call = self._current_max_bet() - p.current_bet
            min_raise_to = self._current_max_bet() + self.min_raise
            if amount < min_raise_to - p.current_bet and amount < p.chips:
                amount = min(min_raise_to - p.current_bet, p.chips)

            amount = min(amount, p.chips)
            p.chips -= amount
            p.current_bet += amount
            p.total_bet_in_hand += amount
            self.pot += amount
            raise_size = p.current_bet - self._current_max_bet()
            if raise_size > 0:
                self.min_raise = max(self.min_raise, raise_size)
            self.last_raiser_idx = self.current_player_idx
            self.players_acted_this_round = {p.telegram_id}
            if p.chips == 0:
                p.is_all_in = True
            result["ok"] = True
            result["msg"] = f"{p.username} raises to {p.current_bet}."

        elif action == "allin":
            amount = p.chips
            to_call = self._current_max_bet() - p.current_bet
            p.chips = 0
            p.current_bet += amount
            p.total_bet_in_hand += amount
            self.pot += amount
            p.is_all_in = True
            if p.current_bet > self._current_max_bet():
                # It's effectively a raise
                raise_size = p.current_bet - self._current_max_bet()
                if raise_size >= self.min_raise:
                    self.min_raise = raise_size
                    self.last_raiser_idx = self.current_player_idx
                    self.players_acted_this_round = {p.telegram_id}
            result["ok"] = True
            result["msg"] = f"{p.username} is ALL IN for {amount}!"

        else:
            result["msg"] = f"Unknown action: {action}"
            return result

        if not result["ok"]:
            return result

        # Track acted
        self.players_acted_this_round.add(p.telegram_id)
        self.actions_this_round += 1

        # Check if only one player remains
        if len(self.active_players) == 1:
            result["hand_over"] = True
            return result

        # Advance to next player
        nxt = self._next_active_seat(self.current_player_idx)
        if nxt is None or self._is_round_complete():
            result["advance_phase"] = True
        else:
            self.current_player_idx = nxt

        return result

    def _is_round_complete(self) -> bool:
        """Check if current betting round is over."""
        active = self.active_non_allin
        if not active:
            return True

        max_bet = self._current_max_bet()
        for p in active:
            if p.telegram_id not in self.players_acted_this_round:
                return False
            if p.current_bet != max_bet:
                return False

        return True

    # ── Phase advancement ──────────────────────────────────

    def advance_phase(self) -> str:
        """
        Move to the next phase. Returns new phase name.
        Deals community cards as needed.
        """
        # Reset round betting state
        for p in self.players:
            p.current_bet = 0
        self.actions_this_round = 0
        self.players_acted_this_round = set()
        self.last_raiser_idx = None
        self.min_raise = self.big_blind

        if self.phase == "preflop":
            self.phase = "flop"
            self.community_cards.extend([self.deck.pop() for _ in range(3)])
        elif self.phase == "flop":
            self.phase = "turn"
            self.community_cards.append(self.deck.pop())
        elif self.phase == "turn":
            self.phase = "river"
            self.community_cards.append(self.deck.pop())
        elif self.phase == "river":
            self.phase = "showdown"
            return self.phase

        # Set first to act post-flop: first active non-allin left of dealer
        first = self._next_active_seat(self.dealer_idx)
        if first is not None:
            self.current_player_idx = first

        # If all remaining players are all-in, keep advancing
        if not self.active_non_allin:
            return self.advance_phase()

        return self.phase

    def run_to_showdown(self) -> None:
        """Deal remaining community cards when everyone is all-in."""
        while len(self.community_cards) < 5:
            if self.phase == "preflop":
                self.community_cards.extend([self.deck.pop() for _ in range(3)])
                self.phase = "flop"
            elif self.phase in ("flop",):
                self.community_cards.append(self.deck.pop())
                self.phase = "turn"
            elif self.phase in ("turn",):
                self.community_cards.append(self.deck.pop())
                self.phase = "river"
            else:
                break
        self.phase = "showdown"

    # ── Showdown / Winner determination ────────────────────

    def determine_winners(self) -> list[dict]:
        """
        Resolve the hand.  Returns list of
        ``{player_id, username, amount, hand_name}``.
        Handles side pots.
        """
        results: dict[int, int] = {}  # player_id → total won

        # If only one active player, they win the whole pot
        active = self.active_players
        if len(active) == 1:
            winner = active[0]
            results[winner.telegram_id] = self.pot
            return [{"player_id": winner.telegram_id,
                     "username": winner.username,
                     "amount": self.pot,
                     "hand_name": "Last player standing"}]

        # Build contribution map
        contribs = {p.telegram_id: p.total_bet_in_hand for p in self.players}
        folded = {p.telegram_id for p in self.players if p.is_folded}

        pots = calculate_pots(contribs, folded)

        # Evaluate hands
        player_cards = {
            p.telegram_id: p.hole_cards
            for p in self.active_players
        }

        # Award each pot
        for pot_amount, eligible_ids in pots:
            # Rank eligible players
            eligible_cards = {pid: player_cards[pid] for pid in eligible_ids if pid in player_cards}
            if not eligible_cards:
                continue

            tiers = compare_hands(eligible_cards, self.community_cards)
            winners = tiers[0]  # top tier
            share = pot_amount // len(winners)
            remainder = pot_amount - share * len(winners)

            for pid in winners:
                results[pid] = results.get(pid, 0) + share
            # Give remainder to first winner
            if remainder > 0:
                results[winners[0]] = results.get(winners[0], 0) + remainder

        # Build output
        output = []
        for pid, won in results.items():
            p = next((pl for pl in self.players if pl.telegram_id == pid), None)
            if p is None:
                continue
            hand_name = ""
            if pid in player_cards:
                sc = best_hand(player_cards[pid] + self.community_cards)
                hand_name = hand_rank_name(sc)
            output.append({
                "player_id": pid,
                "username": p.username,
                "amount": won,
                "hand_name": hand_name,
            })

        # Apply winnings to chip stacks
        for entry in output:
            p = next(pl for pl in self.players if pl.telegram_id == entry["player_id"])
            p.chips += entry["amount"]

        self.pot = 0
        return output

    # ── Elimination check ──────────────────────────────────

    def eliminate_busted_players(self) -> list[PlayerState]:
        """Mark players with 0 chips as eliminated. Return eliminated list."""
        eliminated = []
        for p in self.players:
            if p.chips <= 0 and not p.is_eliminated:
                p.is_eliminated = True
                eliminated.append(p)
        return eliminated

    def is_tournament_over(self) -> bool:
        return len(self.alive_players) <= 1

    def get_final_standings(self) -> list[dict]:
        """
        Return ordered standings from the tournament.
        Earlier eliminations = worse placement.
        """
        alive = [p for p in self.players if not p.is_eliminated]
        eliminated = [p for p in self.players if p.is_eliminated]

        standings = []
        position = 1
        for p in alive:
            standings.append({
                "position": position,
                "telegram_id": p.telegram_id,
                "username": p.username,
                "chips": p.chips,
            })
            position += 1

        # Eliminated players — last eliminated = best finish
        for p in reversed(eliminated):
            standings.append({
                "position": position,
                "telegram_id": p.telegram_id,
                "username": p.username,
                "chips": 0,
            })
            position += 1

        return standings

    # ── Table display ──────────────────────────────────────

    def render_table(self, show_cards: bool = False) -> str:
        """Render the current table state for display in group chat."""
        lines = []
        lines.append("🃏 <b>Texas Hold'em — Spin & Gold</b>")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"💰 Prize: ${self.prize_pool_usd:,.2f}  |  "
            f"Blinds: {self.small_blind}/{self.big_blind}  |  "
            f"Hand #{self.hand_number}"
        )

        # Board
        if self.community_cards:
            board_str = card_display(self.community_cards)
        else:
            board_str = "—"
        board_placeholders = "  —" * (5 - len(self.community_cards))
        lines.append(f"\n📋 Board: {board_str}{board_placeholders}")
        lines.append(f"🏦 Pot: {self.pot}")

        # Players
        lines.append("")
        for i, p in enumerate(self.players):
            if p.is_eliminated:
                tag = "💀"
            elif p.is_folded:
                tag = "🪦"
            elif p.is_all_in:
                tag = "🔥"
            elif i == self.current_player_idx and self.phase not in ("showdown", "finished"):
                tag = "👉"
            else:
                tag = "👤"

            dealer = " [D]" if i == self.dealer_idx and not p.is_eliminated else ""
            bet_str = f"  (bet: {p.current_bet})" if p.current_bet > 0 else ""
            cards_str = ""
            if show_cards and p.hole_cards and not p.is_folded and not p.is_eliminated:
                cards_str = f"  [{card_display(p.hole_cards)}]"

            status = ""
            if p.is_eliminated:
                status = " — eliminated"
            elif p.is_folded:
                status = " — folded"
            elif p.is_all_in:
                status = " — ALL IN"

            lines.append(
                f"{tag} @{p.username} — {p.chips} chips{dealer}{bet_str}{cards_str}{status}"
            )

        if self.phase not in ("showdown", "finished") and self.current_player:
            cp = self.current_player
            actions = self.available_actions()
            lines.append(f"\n⏳ @{cp.username}'s turn  →  {', '.join(actions)}")

        return "\n".join(lines)

    def render_showdown(self, results: list[dict]) -> str:
        """Render showdown results."""
        lines = ["🏆 <b>SHOWDOWN</b>", "━━━━━━━━━━━━━━"]

        if self.community_cards:
            lines.append(f"Board: {card_display(self.community_cards)}")
            lines.append("")

        for r in results:
            # Show hole cards for active players
            p = next((pl for pl in self.players if pl.telegram_id == r["player_id"]), None)
            cards_str = ""
            if p and p.hole_cards and not p.is_folded:
                cards_str = f"  [{card_display(p.hole_cards)}]"
            lines.append(
                f"💰 @{r['username']}{cards_str} — won {r['amount']} chips"
                + (f"  ({r['hand_name']})" if r.get("hand_name") else "")
            )

        return "\n".join(lines)
