from __future__ import annotations

"""安定化した Monte Carlo CFR 近似でヘッズアップ用戦略表を作る CLI です。"""

import argparse
import json
import random
import sys
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
from app.strategy_tables.lib import encode_infoset
from app.strategy_tables.preflop_blueprint import (
    blend_with_blueprint,
    build_preflop_blueprint,
    normalize_weights,
)


class SilentHoldemGame(HoldemGame):
    """学習中はハンドログを書かない軽量版です。"""

    def persist_hand_log(self) -> None:  # pragma: no cover - 副作用抑止だけの差し替え
        return


@lru_cache(maxsize=4)
def load_runtime_strategy_table(table_path: str | None = None) -> dict:
    if table_path:
        path = Path(table_path).expanduser().resolve()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

    sibling_jsons = sorted(Path(__file__).resolve().parent.glob("*.json"))
    if sibling_jsons:
        return json.loads(sibling_jsons[0].read_text(encoding="utf-8"))

    return runtime_table_cpu.load_strategy_table()


def decide_action(game_state, player_state, legal_actions):
    """このファイル自体も CPU として動けるよう、生成済み表を参照します。"""

    table = load_runtime_strategy_table()
    infoset = encode_infoset(game_state, player_state)
    strategy = runtime_table_cpu.lookup_strategy(table, infoset, legal_actions)
    strategy = blend_with_blueprint(
        strategy,
        infoset,
        legal_actions,
        table_weight=0.82,
        game_state=game_state,
        player_state=player_state,
    )
    strategy = runtime_table_cpu.apply_safety_overrides(strategy, infoset, legal_actions)
    action_type = runtime_table_cpu.sample_action(strategy)
    return runtime_table_cpu.materialize_action(action_type, legal_actions, infoset)


class MonteCarloCfrTrainer:
    """安定化用の visit 数・pruning・平滑化を持った簡易 MCCFR です。"""

    def __init__(
        self,
        iterations: int,
        starting_stack: int,
        seed: int | None = None,
        min_visits: int = 25,
        smoothing_alpha: float = 6.0,
    ) -> None:
        self.iterations = iterations
        self.starting_stack = starting_stack
        self.random = random.Random(seed)
        self.min_visits = max(1, min_visits)
        self.smoothing_alpha = max(0.0, smoothing_alpha)
        self.regrets: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.strategy_sums: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.visit_counts: DefaultDict[str, int] = defaultdict(int)

    def train(self) -> dict[str, dict[str, float]]:
        for _iteration in range(self.iterations):
            for update_player in (0, 1):
                self.run_iteration(update_player)
        return self.average_strategy_table()

    def run_iteration(self, update_player: int) -> None:
        game = self.new_game()
        game.start_new_hand(autoplay_cpus=False)

        safety = 0
        while not game.awaiting_new_hand and safety < 400:
            safety += 1
            if game.current_turn is None:
                break

            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break

            infoset = encode_infoset(game.serialize_for_cpu(), player.to_public_dict(True))
            self.visit_counts[infoset] += 1
            strategy = self.current_strategy(infoset, legal_actions)
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
        while not game.awaiting_new_hand and safety < 400:
            safety += 1
            if game.current_turn is None:
                break
            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break
            infoset = encode_infoset(game.serialize_for_cpu(), player.to_public_dict(True))
            strategy = self.current_strategy(infoset, legal_actions)
            action_type = self.sample_action(strategy, legal_actions)
            payload = self.materialize_action(action_type, legal_actions)
            game.apply_player_action(seat, payload["type"], payload.get("amount"))

    def utility_for(self, game: SilentHoldemGame, seat: int) -> float:
        return float(game.players[seat].stack - self.starting_stack)

    def new_game(self) -> SilentHoldemGame:
        game = SilentHoldemGame(BASE_DIR / "logs", BASE_DIR / "embedded_cpus")
        game.configure_table(self.starting_stack, 1)
        for player in game.players[:2]:
            player.is_human = False
            player.cpu_path = None
        return game

    def current_strategy(self, infoset: str, legal_actions: list[dict]) -> dict[str, float]:
        positive = {
            action["type"]: max(0.0, self.regrets[infoset].get(action["type"], 0.0))
            for action in legal_actions
        }
        total = sum(positive.values())
        if total > 0:
            regret_strategy = {action_type: value / total for action_type, value in positive.items()}
            visits = self.visit_counts.get(infoset, 0)
            table_weight = min(0.92, 0.45 + visits / 120.0)
            return blend_with_blueprint(regret_strategy, infoset, legal_actions, table_weight=table_weight)

        blueprint = build_preflop_blueprint(infoset, legal_actions)
        if blueprint:
            return blueprint
        uniform = 1.0 / len(legal_actions)
        return {action["type"]: uniform for action in legal_actions}

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

    def strategy_prior(self, infoset: str, actions: list[str]) -> dict[str, float]:
        phase, _player_count, _position, bucket, pressure, stack_bucket, _texture = infoset.split("|")
        blueprint = build_preflop_blueprint(
            infoset,
            [{"type": action} for action in actions],
        )
        if blueprint:
            return {action: max(0.05, blueprint.get(action, 0.0) * 12.0) for action in actions}

        prior = {action: 1.0 for action in actions}
        weak_bucket = bucket in {"weak", "air", "marginal"}
        medium_bucket = bucket in {"medium", "draw", "speculative"}
        premium_bucket = bucket in {"premium", "strong", "monster", "made", "strong_pair"}
        jammed = pressure == "jam"

        for action in actions:
            if action in {"check", "call"}:
                prior[action] = 3.0 if weak_bucket else 2.0
            elif action == "fold":
                prior[action] = 2.4 if weak_bucket and not jammed else 1.2
            elif action in {"bet", "raise"}:
                if weak_bucket:
                    prior[action] = 0.45
                elif medium_bucket:
                    prior[action] = 0.9
                elif premium_bucket:
                    prior[action] = 1.8
            elif action == "all-in":
                if weak_bucket and not jammed:
                    prior[action] = 0.05
                elif medium_bucket and stack_bucket != "shallow" and not jammed:
                    prior[action] = 0.12
                elif premium_bucket or jammed:
                    prior[action] = 0.9
                else:
                    prior[action] = 0.25

        if phase != "preflop" and weak_bucket and not jammed and "all-in" in prior:
            prior["all-in"] *= 0.5
        return prior

    def smooth_strategy(
        self, infoset: str, weights: dict[str, float], visits: int
    ) -> dict[str, float]:
        actions = sorted(weights)
        prior = self.strategy_prior(infoset, actions)
        smoothed = {}
        alpha = self.smoothing_alpha
        for action in actions:
            smoothed[action] = weights[action] + alpha * prior.get(action, 1.0)
        normalized = normalize_weights(smoothed)
        blend_weight = min(0.9, max(0.55, visits / 160.0))
        return blend_with_blueprint(normalized, infoset, [{"type": action} for action in actions], table_weight=blend_weight)

    def average_strategy_table(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for infoset, weights in self.strategy_sums.items():
            visits = self.visit_counts.get(infoset, 0)
            if visits < self.min_visits:
                continue
            smoothed = self.smooth_strategy(infoset, dict(weights), visits)
            table[infoset] = {
                action: round(value, 6)
                for action, value in sorted(smoothed.items())
            }
        return table

    def pruned_visit_counts(self) -> dict[str, int]:
        return {
            infoset: count
            for infoset, count in sorted(self.visit_counts.items())
            if count >= self.min_visits
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a stabilized heads-up strategy table with approximate Monte Carlo CFR."
    )
    parser.add_argument("--iterations", type=int, default=20000, help="CFR iterations")
    parser.add_argument("--stack", type=int, default=2000, help="Starting stack")
    parser.add_argument(
        "--out",
        default=str(
            BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "stable_heads_up_cfr.json"
        ),
        help="Output JSON path",
    )
    parser.add_argument("--seed", type=int, default=7, help="Random seed")
    parser.add_argument(
        "--print-every",
        type=int,
        default=1000,
        help="Progress interval. Set 0 to disable progress output.",
    )
    parser.add_argument(
        "--min-visits",
        type=int,
        default=25,
        help="Discard infosets visited fewer than this count.",
    )
    parser.add_argument(
        "--smoothing-alpha",
        type=float,
        default=6.0,
        help="Pseudo-count strength used when smoothing strategy probabilities.",
    )
    args = parser.parse_args()

    trainer = MonteCarloCfrTrainer(
        iterations=args.iterations,
        starting_stack=args.stack,
        seed=args.seed,
        min_visits=args.min_visits,
        smoothing_alpha=args.smoothing_alpha,
    )

    if args.print_every > 0:
        for chunk_start in range(0, args.iterations, args.print_every):
            chunk = min(args.print_every, args.iterations - chunk_start)
            trainer.iterations = chunk
            trainer.train()
            trainer.iterations = args.iterations
            done = chunk_start + chunk
            print(f"[stable-cfr] {done}/{args.iterations} iterations completed", file=sys.stderr)
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
        "starting_stack": args.stack,
        "infosets": len(table),
        "visit_entries": len(visit_counts),
        "min_visits": args.min_visits,
        "smoothing_alpha": args.smoothing_alpha,
        "output": str(out_path),
        "visits_output": str(visits_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
