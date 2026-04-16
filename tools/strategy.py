from __future__ import annotations

"""Monte Carlo CFR 近似でヘッズアップ用の戦略表 JSON を生成する CLI です。"""

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
from app.strategy_tables.lib import encode_infoset
from app.sample_cpus import strategy_table_cpu as runtime_table_cpu


class SilentHoldemGame(HoldemGame):
    """CFR 実行中にハンドログをファイル保存しない軽量版です。"""

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
    """生成済み戦略表を読んで、このファイル自体も CPU として動けるようにします。"""

    table = load_runtime_strategy_table()
    infoset = encode_infoset(game_state, player_state)
    strategy = runtime_table_cpu.lookup_strategy(table, infoset, legal_actions)
    action_type = runtime_table_cpu.sample_action(strategy)
    return runtime_table_cpu.materialize_action(action_type, legal_actions, infoset)


class MonteCarloCfrTrainer:
    """現在の抽象化 infoset に合わせて近似戦略表を作る簡易 MCCFR です。"""

    def __init__(self, iterations: int, starting_stack: int, seed: int | None = None) -> None:
        self.iterations = iterations
        self.starting_stack = starting_stack
        self.random = random.Random(seed)
        self.regrets: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.strategy_sums: DefaultDict[str, DefaultDict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )

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

    def evaluate_actions(self, game: SilentHoldemGame, seat: int, legal_actions: list[dict]) -> dict[str, float]:
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
            return {action_type: value / total for action_type, value in positive.items()}
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

    def average_strategy_table(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for infoset, weights in self.strategy_sums.items():
            total = sum(weights.values())
            if total <= 0:
                continue
            table[infoset] = {
                action: round(value / total, 6)
                for action, value in sorted(weights.items())
            }
        return table


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a heads-up strategy table with approximate Monte Carlo CFR."
    )
    parser.add_argument("--iterations", type=int, default=5000, help="CFR iterations")
    parser.add_argument("--stack", type=int, default=2000, help="Starting stack")
    parser.add_argument(
        "--out",
        default=str(BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "cfr_generated.json"),
        help="Output JSON path",
    )
    parser.add_argument("--seed", type=int, default=7, help="Random seed")
    parser.add_argument(
        "--print-every",
        type=int,
        default=1000,
        help="Progress interval. Set 0 to disable progress output.",
    )
    args = parser.parse_args()

    trainer = MonteCarloCfrTrainer(
        iterations=args.iterations,
        starting_stack=args.stack,
        seed=args.seed,
    )

    if args.print_every > 0:
        for chunk_start in range(0, args.iterations, args.print_every):
            chunk = min(args.print_every, args.iterations - chunk_start)
            trainer.iterations = chunk
            trainer.train()
            trainer.iterations = args.iterations
            done = chunk_start + chunk
            print(f"[cfr] {done}/{args.iterations} iterations completed", file=sys.stderr)
    else:
        trainer.train()

    table = trainer.average_strategy_table()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "iterations": args.iterations,
        "starting_stack": args.stack,
        "infosets": len(table),
        "output": str(out_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
