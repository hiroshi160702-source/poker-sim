from __future__ import annotations

import itertools
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .cpu_loader import CpuAgentError, CpuLoader

RANK_ORDER = "23456789TJQKA"
RANK_VALUES = {rank: index + 2 for index, rank in enumerate(RANK_ORDER)}
SUITS = "SHDC"
HAND_NAMES = {
    8: "Straight Flush",
    7: "Four of a Kind",
    6: "Full House",
    5: "Flush",
    4: "Straight",
    3: "Three of a Kind",
    2: "Two Pair",
    1: "One Pair",
    0: "High Card",
}
CPU_NAME_POOL = [
    "CPU North",
    "CPU NorthEast",
    "CPU East",
    "CPU SouthEast",
    "CPU South",
    "CPU SouthWest",
    "CPU West",
    "CPU NorthWest",
]
CPU_TEMPLATE_POOL = [
    "cfr_agent.py",
    "random_agent.py",
    "tight_agent.py",
    "cfr_agent.py",
    "random_agent.py",
    "tight_agent.py",
    "cfr_agent.py",
    "tight_agent.py",
]


def now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")


def make_deck() -> List[str]:
    return [f"{rank}{suit}" for suit in SUITS for rank in RANK_ORDER]


def card_rank(card: str) -> int:
    return RANK_VALUES[card[0]]


def describe_action(action_type: str, amount: int = 0) -> str:
    labels = {
        "fold": "Fold",
        "check": "Check",
        "call": f"Call {amount}",
        "bet": f"Bet {amount}",
        "raise": f"Raise to {amount}",
        "all-in": f"All-in {amount}",
    }
    return labels.get(action_type, action_type.title())


def evaluate_five(cards: Sequence[str]) -> Tuple[int, List[int]]:
    ranks = sorted((card_rank(card) for card in cards), reverse=True)
    rank_counts: Dict[int, int] = {}
    suits = [card[1] for card in cards]
    for rank in ranks:
        rank_counts[rank] = rank_counts.get(rank, 0) + 1

    unique_ranks = sorted(rank_counts.keys(), reverse=True)
    is_flush = len(set(suits)) == 1

    straight_high = 0
    straight_ranks = sorted(set(ranks), reverse=True)
    if len(straight_ranks) == 5 and straight_ranks[0] - straight_ranks[4] == 4:
        straight_high = straight_ranks[0]
    elif straight_ranks == [14, 5, 4, 3, 2]:
        straight_high = 5

    if is_flush and straight_high:
        return 8, [straight_high]

    ordered_counts = sorted(
        rank_counts.items(), key=lambda item: (item[1], item[0]), reverse=True
    )
    counts = sorted(rank_counts.values(), reverse=True)
    ordered_ranks = [rank for rank, _ in ordered_counts]

    if counts == [4, 1]:
        kicker = [rank for rank in ranks if rank != ordered_ranks[0]][0]
        return 7, [ordered_ranks[0], kicker]

    if counts == [3, 2]:
        return 6, ordered_ranks

    if is_flush:
        return 5, sorted(ranks, reverse=True)

    if straight_high:
        return 4, [straight_high]

    if counts == [3, 1, 1]:
        kickers = sorted((rank for rank in ranks if rank != ordered_ranks[0]), reverse=True)
        return 3, [ordered_ranks[0], *kickers]

    if counts == [2, 2, 1]:
        pair_ranks = sorted((rank for rank, count in rank_counts.items() if count == 2), reverse=True)
        kicker = [rank for rank, count in rank_counts.items() if count == 1][0]
        return 2, [*pair_ranks, kicker]

    if counts == [2, 1, 1, 1]:
        pair_rank = ordered_ranks[0]
        kickers = sorted((rank for rank in ranks if rank != pair_rank), reverse=True)
        return 1, [pair_rank, *kickers]

    return 0, sorted(ranks, reverse=True)


def best_hand(cards: Sequence[str]) -> Tuple[Tuple[int, List[int]], str]:
    best_rank: Optional[Tuple[int, List[int]]] = None
    for combo in itertools.combinations(cards, 5):
        rank = evaluate_five(combo)
        if best_rank is None or rank > best_rank:
            best_rank = rank

    assert best_rank is not None
    return best_rank, HAND_NAMES[best_rank[0]]


@dataclass
class PlayerState:
    seat: int
    name: str
    stack: int
    is_human: bool = False
    cpu_path: Optional[str] = None
    hand: List[str] = field(default_factory=list)
    in_hand: bool = True
    folded: bool = False
    all_in: bool = False
    bet_round: int = 0
    total_bet: int = 0
    last_action: str = "Waiting"
    won_last: bool = False
    win_amount: int = 0
    hand_label: str = ""
    cpu_error: Optional[str] = None

    def reset_for_hand(self) -> None:
        self.hand = []
        self.in_hand = self.stack > 0
        self.folded = False
        self.all_in = False
        self.bet_round = 0
        self.total_bet = 0
        self.last_action = "Waiting"
        self.won_last = False
        self.win_amount = 0
        self.hand_label = ""
        self.cpu_error = None

    def to_public_dict(self, reveal_cards: bool = False) -> dict:
        payload = {
            "seat": self.seat,
            "name": self.name,
            "stack": self.stack,
            "is_human": self.is_human,
            "cpu_path": self.cpu_path,
            "hand": self.hand if (reveal_cards or self.is_human) else ["??", "??"],
            "in_hand": self.in_hand,
            "folded": self.folded,
            "all_in": self.all_in,
            "bet_round": self.bet_round,
            "total_bet": self.total_bet,
            "last_action": self.last_action,
            "won_last": self.won_last,
            "win_amount": self.win_amount,
            "hand_label": self.hand_label,
            "cpu_error": self.cpu_error,
        }
        if reveal_cards:
            payload["actual_hand"] = self.hand
        return payload


class HoldemGame:
    def __init__(self, logs_dir: Path, embedded_cpu_dir: Optional[Path] = None) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.embedded_cpu_dir = embedded_cpu_dir or (logs_dir.parent / "embedded_cpus")
        self.embedded_cpu_dir.mkdir(parents=True, exist_ok=True)
        self.cpu_loader = CpuLoader()
        self.small_blind = 25
        self.big_blind = 50
        self.starting_stack = 2000
        self.cpu_count = 5
        self.players = self._build_default_players()
        self.hand_id = 0
        self.dealer_index = -1
        self.community_cards: List[str] = []
        self.deck: List[str] = []
        self.phase = "waiting"
        self.current_turn: Optional[int] = None
        self.current_bet = 0
        self.last_raise_size = self.big_blind
        self.min_raise_to = self.big_blind
        self.pot = 0
        self.pending_to_act: set[int] = set()
        self.log_lines: List[str] = []
        self.history: List[dict] = []
        self.last_winners: List[dict] = []
        self.small_blind_seat: Optional[int] = None
        self.big_blind_seat: Optional[int] = None
        self.table_message = "New table ready."
        self.awaiting_new_hand = True
        self._cached_human_win_rate: float = 0.0
        self._win_rate_dirty = True
        self.autoplay_cpus_enabled = True

    def _build_default_players(self) -> List[PlayerState]:
        sample_dir = (Path(__file__).resolve().parent / "sample_cpus").resolve()
        players = [PlayerState(seat=0, name="You", stack=self.starting_stack, is_human=True)]
        for index in range(self.cpu_count):
            players.append(
                PlayerState(
                    seat=index + 1,
                    name=CPU_NAME_POOL[index],
                    stack=self.starting_stack,
                    cpu_path=str(sample_dir / CPU_TEMPLATE_POOL[index]),
                )
            )
        return players

    def configure_table(self, starting_stack: int, cpu_count: int) -> None:
        if not 500 <= starting_stack <= 50000:
            raise ValueError("Starting stack must be between 500 and 50000.")
        if not 1 <= cpu_count <= 8:
            raise ValueError("CPU count must be between 1 and 8.")

        self.starting_stack = starting_stack
        self.cpu_count = cpu_count
        self.reset_table()
        self.table_message = (
            f"Table configured: {cpu_count + 1} players, starting stack {starting_stack}."
        )

    def reset_table(self) -> None:
        self.players = self._build_default_players()
        self.hand_id = 0
        self.dealer_index = -1
        self.community_cards = []
        self.deck = []
        self.phase = "waiting"
        self.current_turn = None
        self.current_bet = 0
        self.last_raise_size = self.big_blind
        self.min_raise_to = self.big_blind
        self.pot = 0
        self.pending_to_act = set()
        self.log_lines = []
        self.history = []
        self.last_winners = []
        self.small_blind_seat = None
        self.big_blind_seat = None
        self.table_message = "Table reset."
        self.awaiting_new_hand = True
        self._win_rate_dirty = True
        self.autoplay_cpus_enabled = True

    def add_log(self, message: str) -> None:
        self.log_lines.append(f"[{now_label()}] {message}")
        self.log_lines = self.log_lines[-120:]

    def eligible_seats(self) -> List[int]:
        return [player.seat for player in self.players if player.stack > 0]

    def active_players(self) -> List[PlayerState]:
        return [player for player in self.players if player.in_hand and not player.folded]

    def seats_in_order_from(self, start_seat: int) -> List[int]:
        total = len(self.players)
        return [((start_seat + offset) % total) for offset in range(total)]

    def next_occupied_seat(self, start_seat: int) -> Optional[int]:
        for seat in self.seats_in_order_from(start_seat + 1):
            if self.players[seat].stack > 0:
                return seat
        return None

    def next_eligible_actor(self, start_seat: int) -> Optional[int]:
        for seat in self.seats_in_order_from(start_seat + 1):
            player = self.players[seat]
            if player.in_hand and not player.folded and not player.all_in:
                return seat
        return None

    def start_new_hand(self, autoplay_cpus: bool = True) -> None:
        eligible = self.eligible_seats()
        if len(eligible) < 2:
            self.table_message = "At least two players with chips are required."
            self.awaiting_new_hand = True
            return

        self.hand_id += 1
        self.community_cards = []
        self.deck = make_deck()
        random.shuffle(self.deck)
        self.phase = "preflop"
        self.current_bet = 0
        self.last_raise_size = self.big_blind
        self.min_raise_to = self.big_blind
        self.pot = 0
        self.pending_to_act = set()
        self.last_winners = []
        self.awaiting_new_hand = False
        self.autoplay_cpus_enabled = autoplay_cpus
        self.table_message = f"Hand #{self.hand_id} started."
        self._win_rate_dirty = True

        dealer_start = self.dealer_index if self.dealer_index >= 0 else eligible[-1]
        self.dealer_index = self.next_occupied_seat(dealer_start) or eligible[0]

        for player in self.players:
            player.reset_for_hand()

        for _ in range(2):
            for seat in self.seats_in_order_from(self.dealer_index + 1):
                player = self.players[seat]
                if player.stack > 0:
                    player.hand.append(self.deck.pop())

        if len(eligible) == 2:
            small_blind_seat = self.dealer_index
            big_blind_seat = self.next_occupied_seat(self.dealer_index) or self.dealer_index
        else:
            small_blind_seat = self.next_occupied_seat(self.dealer_index) or self.dealer_index
            big_blind_seat = self.next_occupied_seat(small_blind_seat) or small_blind_seat
        self.small_blind_seat = small_blind_seat
        self.big_blind_seat = big_blind_seat

        self.post_blind(small_blind_seat, self.small_blind, "small blind")
        self.post_blind(big_blind_seat, self.big_blind, "big blind")

        self.current_bet = max(self.players[big_blind_seat].bet_round, self.big_blind)
        self.last_raise_size = self.big_blind
        self.min_raise_to = self.current_bet + self.last_raise_size
        self.pending_to_act = {
            player.seat
            for player in self.players
            if player.in_hand and not player.folded and not player.all_in
        }
        self.current_turn = self.next_eligible_actor(big_blind_seat)
        self.add_log(
            f"Hand #{self.hand_id} begins. Dealer: {self.players[self.dealer_index].name}, "
            f"SB: {self.players[small_blind_seat].name}, BB: {self.players[big_blind_seat].name}."
        )
        if autoplay_cpus:
            self.auto_play_until_human()

    def post_blind(self, seat: int, amount: int, label: str) -> None:
        player = self.players[seat]
        posted = min(player.stack, amount)
        player.stack -= posted
        player.bet_round += posted
        player.total_bet += posted
        player.all_in = player.stack == 0
        player.last_action = f"{label.title()} {posted}"
        self.pot += posted

    def legal_actions_for(self, seat: int) -> List[dict]:
        player = self.players[seat]
        if self.awaiting_new_hand or self.phase == "showdown":
            return []
        if not player.in_hand or player.folded or player.all_in:
            return []

        to_call = max(0, self.current_bet - player.bet_round)
        actions: List[dict] = []

        if to_call > 0:
            actions.append({"type": "fold", "label": "Fold"})
            actions.append(
                {
                    "type": "call",
                    "label": f"Call {min(to_call, player.stack)}",
                    "amount": min(to_call, player.stack),
                }
            )
            if player.stack > 0:
                actions.append(
                    {"type": "all-in", "label": f"All-in {player.stack}", "amount": player.stack}
                )
        else:
            actions.append({"type": "check", "label": "Check"})
            if player.stack > 0:
                actions.append(
                    {"type": "all-in", "label": f"All-in {player.stack}", "amount": player.stack}
                )

        total_max = player.bet_round + player.stack
        if player.stack > to_call and total_max > self.current_bet:
            if self.current_bet == 0:
                min_total = min(total_max, self.big_blind)
                actions.append(
                    {
                        "type": "bet",
                        "label": f"Bet {min_total}+",
                        "min_total": min_total,
                        "max_total": total_max,
                    }
                )
            else:
                min_total = min(total_max, self.current_bet + self.last_raise_size)
                if total_max > self.current_bet:
                    actions.append(
                        {
                            "type": "raise",
                            "label": f"Raise to {min_total}+",
                            "min_total": min_total,
                            "max_total": total_max,
                        }
                    )

        return actions

    def apply_player_action(self, seat: int, action_type: str, amount: Optional[int] = None) -> None:
        if seat != self.current_turn:
            raise ValueError("Not this player's turn.")

        player = self.players[seat]
        legal_types = {action["type"] for action in self.legal_actions_for(seat)}
        if action_type not in legal_types:
            raise ValueError(f"Illegal action: {action_type}")

        to_call = max(0, self.current_bet - player.bet_round)
        reset_pending = False
        action_amount = 0

        if action_type == "fold":
            player.folded = True
            player.in_hand = False
            player.last_action = "Fold"
            self.pending_to_act.discard(seat)
        elif action_type == "check":
            player.last_action = "Check"
            self.pending_to_act.discard(seat)
        elif action_type == "call":
            action_amount = min(to_call, player.stack)
            self.commit_chips(player, action_amount)
            player.last_action = describe_action("call", action_amount)
            self.pending_to_act.discard(seat)
        elif action_type == "bet":
            target_total = self.normalize_target_total(player, amount, opening=True)
            action_amount = target_total - player.bet_round
            self.commit_chips(player, action_amount)
            self.current_bet = player.bet_round
            self.last_raise_size = max(self.big_blind, self.current_bet)
            self.min_raise_to = self.current_bet + self.last_raise_size
            player.last_action = describe_action("bet", player.bet_round)
            reset_pending = True
        elif action_type == "raise":
            target_total = self.normalize_target_total(player, amount, opening=False)
            action_amount = target_total - player.bet_round
            previous_bet = self.current_bet
            self.commit_chips(player, action_amount)
            self.current_bet = player.bet_round
            self.last_raise_size = max(self.big_blind, self.current_bet - previous_bet)
            self.min_raise_to = self.current_bet + self.last_raise_size
            player.last_action = describe_action("raise", player.bet_round)
            reset_pending = True
        elif action_type == "all-in":
            action_amount = player.stack
            previous_bet = self.current_bet
            self.commit_chips(player, action_amount)
            if player.bet_round > self.current_bet:
                self.current_bet = player.bet_round
                self.last_raise_size = max(self.big_blind, self.current_bet - previous_bet)
                self.min_raise_to = self.current_bet + self.last_raise_size
                player.last_action = describe_action("all-in", player.bet_round)
                reset_pending = True
            else:
                player.last_action = describe_action("all-in", action_amount)
                self.pending_to_act.discard(seat)
        else:
            raise ValueError(f"Unsupported action: {action_type}")

        if player.stack == 0:
            player.all_in = True

        self.add_log(f"{player.name}: {player.last_action}")
        self._win_rate_dirty = True

        if reset_pending:
            self.pending_to_act = {
                contender.seat
                for contender in self.players
                if contender.in_hand and not contender.folded and not contender.all_in and contender.seat != seat
            }

        self.resolve_after_action(seat)

    def normalize_target_total(self, player: PlayerState, amount: Optional[int], opening: bool) -> int:
        total_max = player.bet_round + player.stack
        if amount is None:
            raise ValueError("Bet amount is required.")

        if opening:
            min_total = self.big_blind
        else:
            min_total = self.current_bet + self.last_raise_size

        if total_max <= min_total:
            return total_max

        target_total = max(min_total, min(total_max, amount))
        return target_total

    def commit_chips(self, player: PlayerState, amount: int) -> None:
        committed = min(player.stack, max(0, amount))
        player.stack -= committed
        player.bet_round += committed
        player.total_bet += committed
        self.pot += committed

    def resolve_after_action(self, seat: int) -> None:
        active = self.active_players()
        if len(active) == 1:
            self.finish_without_showdown(active[0])
            return

        if not self.pending_to_act:
            self.advance_phase_or_showdown()
            return

        next_turn = self.next_eligible_actor(seat)
        if next_turn is None:
            self.advance_phase_or_showdown()
            return

        self.current_turn = next_turn

    def advance_phase_or_showdown(self) -> None:
        active = self.active_players()
        if len(active) <= 1:
            self.finish_without_showdown(active[0])
            return

        for player in self.players:
            player.bet_round = 0

        self.current_bet = 0
        self.last_raise_size = self.big_blind
        self.min_raise_to = self.big_blind

        if len([player for player in active if not player.all_in]) <= 1:
            self.runout_remaining_board()
            self.showdown()
            return

        phase_sequence = ["preflop", "flop", "turn", "river"]
        current_index = phase_sequence.index(self.phase)
        if self.phase == "river":
            self.showdown()
            return

        self.phase = phase_sequence[current_index + 1]
        self.deal_board_cards()
        self.pending_to_act = {
            player.seat for player in active if not player.all_in and not player.folded
        }
        self.current_turn = self.next_eligible_actor(self.dealer_index) or next(
            iter(self.pending_to_act)
        )
        self.add_log(f"{self.phase.title()} dealt: {' '.join(self.community_cards)}")
        if self.autoplay_cpus_enabled:
            self.auto_play_until_human()

    def runout_remaining_board(self) -> None:
        while len(self.community_cards) < 5:
            if len(self.community_cards) == 0:
                self.phase = "flop"
            elif len(self.community_cards) == 3:
                self.phase = "turn"
            elif len(self.community_cards) == 4:
                self.phase = "river"
            self.deal_board_cards()
        self.add_log(f"Runout board: {' '.join(self.community_cards)}")

    def deal_board_cards(self) -> None:
        if self.phase == "flop":
            self.community_cards.extend([self.deck.pop(), self.deck.pop(), self.deck.pop()])
        elif self.phase in {"turn", "river"}:
            self.community_cards.append(self.deck.pop())

    def finish_without_showdown(self, winner: PlayerState) -> None:
        winner.stack += self.pot
        winner.won_last = True
        winner.win_amount = self.pot
        self.last_winners = [{"seat": winner.seat, "name": winner.name, "amount": self.pot}]
        self.table_message = f"{winner.name} wins {self.pot} chips by everyone folding."
        self.add_log(self.table_message)
        self.history.insert(
            0,
            {
                "hand_id": self.hand_id,
                "result": self.table_message,
                "pot": self.pot,
                "winners": self.last_winners,
                "community": list(self.community_cards),
            },
        )
        self.history = self.history[:20]
        self.persist_hand_log()
        self.phase = "showdown"
        self.current_turn = None
        self.awaiting_new_hand = True
        self._win_rate_dirty = True

    def showdown(self) -> None:
        rankings: Dict[int, Tuple[Tuple[int, List[int]], str]] = {}
        for player in self.active_players():
            rank, label = best_hand(player.hand + self.community_cards)
            player.hand_label = label
            rankings[player.seat] = (rank, label)

        self.last_winners = []
        payouts = self.compute_side_pots(rankings)
        for seat, amount in payouts.items():
            player = self.players[seat]
            player.stack += amount
            player.won_last = True
            player.win_amount += amount
            self.last_winners.append({"seat": seat, "name": player.name, "amount": amount})

        summary = ", ".join(f"{winner['name']} +{winner['amount']}" for winner in self.last_winners)
        detail = " / ".join(
            f"{self.players[seat].name}: {rankings[seat][1]}" for seat in rankings
        )
        self.table_message = f"Showdown complete. {summary}."
        self.add_log(self.table_message)
        self.add_log(f"Hands: {detail}")
        self.history.insert(
            0,
            {
                "hand_id": self.hand_id,
                "result": self.table_message,
                "pot": self.pot,
                "winners": self.last_winners,
                "community": list(self.community_cards),
                "details": detail,
            },
        )
        self.history = self.history[:20]
        self.persist_hand_log()
        self.phase = "showdown"
        self.current_turn = None
        self.awaiting_new_hand = True
        self._win_rate_dirty = True

    def compute_side_pots(self, rankings: Dict[int, Tuple[Tuple[int, List[int]], str]]) -> Dict[int, int]:
        contributions = {player.seat: player.total_bet for player in self.players if player.total_bet > 0}
        levels = sorted(set(contributions.values()))
        payouts: Dict[int, int] = {}
        previous = 0
        for level in levels:
            involved = [seat for seat, total in contributions.items() if total >= level]
            pot_amount = (level - previous) * len(involved)
            eligible = [
                seat
                for seat in involved
                if seat in rankings and not self.players[seat].folded
            ]
            if eligible:
                best_rank = max(rankings[seat][0] for seat in eligible)
                winners = [seat for seat in eligible if rankings[seat][0] == best_rank]
                share = pot_amount // len(winners)
                remainder = pot_amount % len(winners)
                ordered = self.seats_in_order_from(self.dealer_index + 1)
                for seat in winners:
                    payouts[seat] = payouts.get(seat, 0) + share
                for seat in ordered:
                    if seat in winners and remainder > 0:
                        payouts[seat] += 1
                        remainder -= 1
            previous = level
        return payouts

    def auto_play_until_human(self) -> None:
        safety = 0
        while (
            not self.awaiting_new_hand
            and self.current_turn is not None
            and not self.players[self.current_turn].is_human
            and safety < 100
        ):
            safety += 1
            seat = self.current_turn
            player = self.players[seat]
            legal_actions = self.legal_actions_for(seat)
            try:
                decision = self.cpu_decision_for(player, legal_actions)
            except CpuAgentError as exc:
                player.cpu_error = str(exc)
                self.add_log(f"{player.name} CPU error: {exc}. Falling back to check/fold.")
                decision = self.fallback_decision(legal_actions)

            action_type = decision.get("type", "")
            amount = decision.get("amount")
            try:
                self.apply_player_action(seat, action_type, amount)
            except Exception:
                fallback = self.fallback_decision(legal_actions)
                self.add_log(
                    f"{player.name} returned invalid action. Fallback: {fallback['type']}."
                )
                self.apply_player_action(seat, fallback["type"], fallback.get("amount"))

    def cpu_decision_for(self, player: PlayerState, legal_actions: List[dict]) -> dict:
        if not player.cpu_path:
            return self.fallback_decision(legal_actions)

        decide_action = self.cpu_loader.load(player.cpu_path)
        decision = decide_action(self.serialize_for_cpu(), player.to_public_dict(True), legal_actions)
        if not isinstance(decision, dict) or "type" not in decision:
            raise CpuAgentError("CPU decide_action の返り値は {'type': ..., 'amount': ...} 形式である必要があります。")
        return decision

    def fallback_decision(self, legal_actions: List[dict]) -> dict:
        for preferred in ("check", "call", "all-in", "fold"):
            for action in legal_actions:
                if action["type"] == preferred:
                    payload = {"type": preferred}
                    if "min_total" in action:
                        payload["amount"] = action["min_total"]
                    if "amount" in action:
                        payload["amount"] = action["amount"]
                    return payload
        return {"type": "fold"}

    def load_cpu(self, seat: int, path: str) -> None:
        player = self.players[seat]
        if player.is_human:
            raise ValueError("Human seat does not accept CPU files.")
        resolved = str(Path(path).expanduser().resolve())
        self.cpu_loader.clear_cache(resolved)
        self.cpu_loader.load(resolved)
        player.cpu_path = resolved
        player.cpu_error = None
        self.add_log(f"{player.name} CPU loaded: {resolved}")
        self.auto_play_until_human()

    def save_embedded_cpu(self, seat: int, code: str) -> str:
        player = self.players[seat]
        if player.is_human:
            raise ValueError("Human seat does not accept CPU files.")
        if not code.strip():
            raise ValueError("CPU code cannot be empty.")

        target = self.embedded_cpu_dir / f"seat_{seat}_{player.name.lower().replace(' ', '_')}.py"
        target.write_text(code, encoding="utf-8")
        self.load_cpu(seat, str(target))
        self.add_log(f"{player.name} CPU script saved: {target}")
        return str(target)

    def serialize_for_cpu(self) -> dict:
        return self.serialize_state(reveal_all_cards=True)

    def estimate_win_rates(self, samples: int = 250) -> Dict[int, float]:
        if self.phase == "waiting":
            return {}
        contenders = [player for player in self.active_players()]
        if len(contenders) <= 1:
            return {contenders[0].seat: 100.0} if contenders else {}

        used_cards = set(self.community_cards)
        for player in contenders:
            used_cards.update(player.hand)

        remaining_cards = [card for card in make_deck() if card not in used_cards]
        cards_needed = 5 - len(self.community_cards)
        wins: Dict[int, float] = {player.seat: 0.0 for player in contenders}

        if cards_needed == 0:
            rankings = {
                player.seat: best_hand(player.hand + self.community_cards)[0]
                for player in contenders
            }
            best_rank = max(rankings.values())
            winners = [seat for seat, rank in rankings.items() if rank == best_rank]
            share = 100.0 / len(winners)
            for seat in winners:
                wins[seat] = share
            return wins

        iterations = max(1, samples)
        for _ in range(iterations):
            draw = random.sample(remaining_cards, cards_needed)
            board = self.community_cards + draw
            rankings = {player.seat: best_hand(player.hand + board)[0] for player in contenders}
            best_rank = max(rankings.values())
            winners = [seat for seat, rank in rankings.items() if rank == best_rank]
            share = 1.0 / len(winners)
            for seat in winners:
                wins[seat] += share

        return {seat: round((value / iterations) * 100, 1) for seat, value in wins.items()}

    def human_win_rate(self, samples: int = 250) -> float:
        if not self._win_rate_dirty:
            return self._cached_human_win_rate
        human = next((player for player in self.players if player.is_human), None)
        if human is None:
            return 0.0
        self._cached_human_win_rate = self.estimate_win_rates(samples).get(human.seat, 0.0)
        self._win_rate_dirty = False
        return self._cached_human_win_rate

    def serialize_state(self, reveal_all_cards: bool = False, reveal_folded: bool = False) -> dict:
        human_win_rate = self.human_win_rate()
        return {
            "hand_id": self.hand_id,
            "phase": self.phase,
            "pot": self.pot,
            "community_cards": self.community_cards,
            "current_turn": self.current_turn,
            "current_bet": self.current_bet,
            "min_raise_to": self.min_raise_to,
            "dealer_index": self.dealer_index,
            "awaiting_new_hand": self.awaiting_new_hand,
            "table_message": self.table_message,
            "table_config": {
                "starting_stack": self.starting_stack,
                "cpu_count": self.cpu_count,
                "total_players": len(self.players),
                "allowed_cpu_counts": [1, 2, 3, 4, 5, 6, 7, 8],
            },
            "reveal_folded": reveal_folded,
            "hero_win_rate": human_win_rate,
            "players": [
                {
                    **player.to_public_dict(
                        reveal_all_cards
                        or (self.phase == "showdown" and (reveal_folded or not player.folded))
                    ),
                    "is_current_turn": player.seat == self.current_turn,
                    "is_dealer": player.seat == self.dealer_index,
                    "is_small_blind": player.seat == self.small_blind_seat,
                    "is_big_blind": player.seat == self.big_blind_seat,
                    "legal_actions": self.legal_actions_for(player.seat)
                    if player.seat == self.current_turn and (player.is_human or reveal_all_cards)
                    else [],
                }
                for player in self.players
            ],
            "logs": self.log_lines[-40:],
            "history": self.history[:12],
            "last_winners": self.last_winners,
        }

    def persist_hand_log(self) -> None:
        payload = {
            "hand_id": self.hand_id,
            "timestamp": datetime.now().isoformat(),
            "phase": self.phase,
            "community_cards": self.community_cards,
            "pot": self.pot,
            "players": [player.to_public_dict(True) for player in self.players],
            "logs": self.log_lines[-60:],
            "winners": self.last_winners,
        }
        target = self.logs_dir / f"hand_{self.hand_id:04d}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
