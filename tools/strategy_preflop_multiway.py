from __future__ import annotations

"""マルチプレイヤー向けのプリフロップ専用 MCCFR 近似で戦略表を作る CLI です。"""

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import DefaultDict

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.engine import HoldemGame
from app.sample_cpus import strategy_table_cpu as runtime_table_cpu
from app.strategy_tables.lib import candidate_infosets, encode_infoset
from app.strategy_tables.preflop_blueprint import normalize_weights

DEFAULT_BASE_TABLE_CANDIDATES = [
    BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "multiplayer_strategy_6p_5000000hands.json",
    BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "multiway_3p_100000.json",
    BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "tournament_blueprint_heads_up_cfr_blended.json",
]


class SilentHoldemGame(HoldemGame):
    def persist_hand_log(self) -> None:  # pragma: no cover
        return


@lru_cache(maxsize=8)
def load_base_table(table_path: str | None = None) -> dict:
    if table_path:
        path = Path(table_path).expanduser().resolve()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

    for candidate in DEFAULT_BASE_TABLE_CANDIDATES:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))

    return {}


class MultiwayPreflopMccfrTrainer:
    def __init__(
        self,
        *,
        iterations: int,
        player_count: int,
        starting_stack: int,
        seed: int | None = None,
        min_visits: int = 40,
        smoothing_alpha: float = 10.0,
        base_table_path: str | None = None,
        progress_callback=None,
        progress_every: int = 200,
    ) -> None:
        self.iterations = iterations
        self.player_count = player_count
        self.starting_stack = starting_stack
        self.random = random.Random(seed)
        self.min_visits = max(1, min_visits)
        self.smoothing_alpha = max(0.0, smoothing_alpha)
        self.base_table_path = base_table_path
        self.base_table = load_base_table(base_table_path)
        self.progress_callback = progress_callback
        self.progress_every = max(0, progress_every)
        self.regrets: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.strategy_sums: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.visit_counts: DefaultDict[str, int] = defaultdict(int)

    def train(self) -> dict[str, dict[str, float]]:
        started_at = time.perf_counter()
        for iteration_index in range(self.iterations):
            for update_player in range(self.player_count):
                self.run_iteration(update_player)
            completed = iteration_index + 1
            should_report = (
                self.progress_callback
                and self.progress_every > 0
                and (
                    completed == 1
                    or completed == self.iterations
                    or completed % self.progress_every == 0
                )
            )
            if should_report:
                elapsed_seconds = max(0.0, time.perf_counter() - started_at)
                iterations_per_second = completed / elapsed_seconds if elapsed_seconds > 0 else 0.0
                remaining = max(0, self.iterations - completed)
                eta_seconds = (
                    remaining / iterations_per_second if iterations_per_second > 0 else None
                )
                self.progress_callback(
                    {
                        "completed_iterations": completed,
                        "total_iterations": self.iterations,
                        "percent": round((completed / max(1, self.iterations)) * 100, 2),
                        "message": (
                            f"Simulated {completed} / {self.iterations} "
                            f"preflop CFR iterations for {self.player_count} players."
                        ),
                        "elapsed_seconds": round(elapsed_seconds, 1),
                        "estimated_remaining_seconds": (
                            round(eta_seconds, 1) if eta_seconds is not None else None
                        ),
                        "infosets": len(self.visit_counts),
                    }
                )
        return self.average_strategy_table()

    def run_iteration(self, update_player: int) -> None:
        game = self.new_game()
        game.start_new_hand(autoplay_cpus=False)

        safety = 0
        while not game.awaiting_new_hand and safety < 500:
            safety += 1
            if game.current_turn is None:
                break

            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break

            game_state = game.serialize_for_cpu()
            player_state = player.to_public_dict(True)

            if game.phase == "preflop":
                infoset = encode_infoset(game_state, player_state)
                self.visit_counts[infoset] += 1
                strategy = self.current_strategy(
                    infoset,
                    legal_actions,
                    game_state=game_state,
                    player_state=player_state,
                )
                self.accumulate_average_strategy(infoset, legal_actions, strategy)

                if seat == update_player:
                    action_utilities = self.evaluate_actions(game, seat, legal_actions)
                    node_utility = sum(
                        strategy[action["type"]] * action_utilities[action["type"]]
                        for action in legal_actions
                    )
                    for action in legal_actions:
                        action_type = action["type"]
                        self.regrets[infoset][action_type] += (
                            action_utilities[action_type] - node_utility
                        )
            else:
                strategy = self.rollout_strategy(game_state, player_state, legal_actions)

            chosen_action = self.sample_action(strategy, legal_actions)
            payload = self.materialize_action(chosen_action, legal_actions)
            game.apply_player_action(seat, payload["type"], payload.get("amount"))

    def evaluate_actions(
        self, game: SilentHoldemGame, seat: int, legal_actions: list[dict]
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for action in legal_actions:
            branch = deepcopy(game)
            payload = self.materialize_action(action["type"], branch.legal_actions_for(seat))
            branch.apply_player_action(seat, payload["type"], payload.get("amount"))
            self.playout(branch)
            values[action["type"]] = self.utility_for(branch, seat)
        return values

    def playout(self, game: SilentHoldemGame) -> None:
        safety = 0
        while not game.awaiting_new_hand and safety < 800:
            safety += 1
            if game.current_turn is None:
                break
            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break
            game_state = game.serialize_for_cpu()
            player_state = player.to_public_dict(True)
            if game.phase == "preflop":
                infoset = encode_infoset(game_state, player_state)
                strategy = self.current_strategy(
                    infoset,
                    legal_actions,
                    game_state=game_state,
                    player_state=player_state,
                )
            else:
                strategy = self.rollout_strategy(game_state, player_state, legal_actions)
            action_type = self.sample_action(strategy, legal_actions)
            payload = self.materialize_action(action_type, legal_actions)
            game.apply_player_action(seat, payload["type"], payload.get("amount"))

    def utility_for(self, game: SilentHoldemGame, seat: int) -> float:
        return float(game.players[seat].stack - self.starting_stack)

    def new_game(self) -> SilentHoldemGame:
        game = SilentHoldemGame(BASE_DIR / "logs", BASE_DIR / "embedded_cpus")
        game.configure_table(self.starting_stack, self.player_count - 1)
        for player in game.players[: self.player_count]:
            player.is_human = False
            player.cpu_path = None
        return game

    def current_strategy(
        self,
        infoset: str,
        legal_actions: list[dict],
        *,
        game_state: dict | None = None,
        player_state: dict | None = None,
    ) -> dict[str, float]:
        positive = {
            action["type"]: max(0.0, self.regrets[infoset].get(action["type"], 0.0))
            for action in legal_actions
        }
        total = sum(positive.values())
        if total > 0:
            return {
                action_type: value / total for action_type, value in positive.items()
            }
        return self.uniform_strategy(legal_actions)

    def rollout_strategy(
        self, game_state: dict, player_state: dict, legal_actions: list[dict]
    ) -> dict[str, float]:
        infoset = encode_infoset(game_state, player_state)
        base_strategy = self.lookup_base_strategy(infoset, legal_actions)
        if base_strategy:
            return runtime_table_cpu.apply_safety_overrides(base_strategy, infoset, legal_actions)
        return self.passive_fallback_strategy(legal_actions)

    def lookup_base_strategy(
        self, infoset: str, legal_actions: list[dict]
    ) -> dict[str, float] | None:
        legal_types = {action["type"] for action in legal_actions}
        for key in candidate_infosets(infoset):
            strategy = self.base_table.get(key)
            if not strategy:
                continue
            filtered = {
                action: probability
                for action, probability in strategy.items()
                if action in legal_types
            }
            if filtered:
                return normalize_weights(filtered)
        return None

    def passive_fallback_strategy(self, legal_actions: list[dict]) -> dict[str, float]:
        weights = {}
        for action in legal_actions:
            action_type = action["type"]
            if action_type in {"check", "call"}:
                weights[action_type] = 4.0
            elif action_type == "fold":
                weights[action_type] = 2.4
            elif action_type in {"bet", "raise"}:
                weights[action_type] = 0.7
            elif action_type == "all-in":
                weights[action_type] = 0.08
            else:
                weights[action_type] = 1.0
        return normalize_weights(weights)

    def uniform_strategy(self, legal_actions: list[dict]) -> dict[str, float]:
        weight = 1.0 / len(legal_actions)
        return {action["type"]: weight for action in legal_actions}

    def accumulate_average_strategy(
        self, infoset: str, legal_actions: list[dict], strategy: dict[str, float]
    ) -> None:
        for action in legal_actions:
            action_type = action["type"]
            self.strategy_sums[infoset][action_type] += strategy.get(action_type, 0.0)

    def sample_action(self, strategy: dict[str, float], legal_actions: list[dict]) -> str:
        threshold = self.random.random()
        cumulative = 0.0
        for action in legal_actions:
            action_type = action["type"]
            cumulative += strategy.get(action_type, 0.0)
            if threshold <= cumulative:
                return action_type
        return legal_actions[-1]["type"]

    def materialize_action(self, action_type: str, legal_actions: list[dict]) -> dict:
        for action in legal_actions:
            if action["type"] != action_type:
                continue
            payload = {"type": action_type}
            if action_type in {"bet", "raise"}:
                payload["amount"] = action["min_total"]
            elif "amount" in action:
                payload["amount"] = action["amount"]
            return payload
        fallback = legal_actions[0]
        return {"type": fallback["type"], "amount": fallback.get("amount")}

    def strategy_prior(
        self,
        infoset: str,
        actions: list[str],
    ) -> dict[str, float]:
        return {action: 1.0 for action in actions}

    def smooth_strategy(
        self, infoset: str, weights: dict[str, float], visits: int
    ) -> dict[str, float]:
        actions = sorted(weights)
        prior = self.strategy_prior(infoset, actions)
        smoothed = {}
        alpha = self.smoothing_alpha
        for action in actions:
            smoothed[action] = weights[action] + alpha * prior.get(action, 1.0)
        return normalize_weights(smoothed)

    def average_strategy_table(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for infoset, weights in self.strategy_sums.items():
            if not infoset.startswith("preflop|"):
                continue
            visits = self.visit_counts.get(infoset, 0)
            if visits < self.min_visits:
                continue
            smoothed = self.smooth_strategy(infoset, dict(weights), visits)
            table[infoset] = {
                action: round(value, 6) for action, value in sorted(smoothed.items())
            }
        return dict(sorted(table.items()))

    def pruned_visit_counts(self) -> dict[str, int]:
        return {
            infoset: count
            for infoset, count in sorted(self.visit_counts.items())
            if infoset.startswith("preflop|") and count >= self.min_visits
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a multiway preflop strategy table with approximate MCCFR."
    )
    parser.add_argument("--iterations", type=int, default=2000, help="Training iterations")
    parser.add_argument("--players", type=int, default=6, help="Total player count (2-9)")
    parser.add_argument("--stack", type=int, default=5000, help="Starting stack")
    parser.add_argument(
        "--out",
        default=str(
            BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "preflop_multiway_mccfr.json"
        ),
        help="Output JSON path",
    )
    parser.add_argument("--seed", type=int, default=11, help="Random seed")
    parser.add_argument(
        "--print-every",
        type=int,
        default=200,
        help="Progress interval. Set 0 to disable progress output.",
    )
    parser.add_argument(
        "--min-visits",
        type=int,
        default=40,
        help="Discard infosets visited fewer than this count.",
    )
    parser.add_argument(
        "--smoothing-alpha",
        type=float,
        default=10.0,
        help="Pseudo-count strength used when smoothing strategy probabilities.",
    )
    parser.add_argument(
        "--base-table",
        default=None,
        help="Optional warm-start strategy table used before regrets become informative.",
    )
    args = parser.parse_args()

    trainer = MultiwayPreflopMccfrTrainer(
        iterations=args.iterations,
        player_count=args.players,
        starting_stack=args.stack,
        seed=args.seed,
        min_visits=args.min_visits,
        smoothing_alpha=args.smoothing_alpha,
        base_table_path=args.base_table,
    )

    if args.print_every > 0:
        for chunk_start in range(0, args.iterations, args.print_every):
            chunk = min(args.print_every, args.iterations - chunk_start)
            trainer.iterations = chunk
            trainer.train()
            trainer.iterations = args.iterations
            done = chunk_start + chunk
            print(
                f"[multiway-preflop-mccfr] {done}/{args.iterations} iterations completed",
                file=sys.stderr,
            )
    else:
        trainer.train()

    table = trainer.average_strategy_table()
    visit_counts = trainer.pruned_visit_counts()

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")

    visits_path = out_path.with_name(f"{out_path.stem}_visits.json")
    visits_path.write_text(json.dumps(visit_counts, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "iterations": args.iterations,
        "players": args.players,
        "starting_stack": args.stack,
        "infosets": len(table),
        "visit_entries": len(visit_counts),
        "min_visits": args.min_visits,
        "smoothing_alpha": args.smoothing_alpha,
        "base_table": args.base_table,
        "output": str(out_path),
        "visits_output": str(visits_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
