from __future__ import annotations

"""マルチプレイヤー向けの MCCFR 近似で全ストリートの戦略表を作る CLI です。"""

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
from app.strategy_tables.preflop_blueprint import (
    COLOR_TO_INDEX,
    blend_with_blueprint,
    build_preflop_blueprint,
    normalize_weights,
)

SIZE_SUFFIXES = {"small", "medium", "large"}
UPDATE_MODE = "round_robin_traverser_counterfactual_rollout"
NEGATIVE_REGRET_PRUNE_PROBABILITY = 0.95
NEGATIVE_REGRET_PRUNE_MIN_ITERATIONS = 1000
NEGATIVE_REGRET_PRUNE_AVG_GAP_DELTA = 2500.0
LEARNING_SAFETY_ENABLED = True

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
        preflop_only: bool = False,
    ) -> None:
        self.iterations = iterations
        self.player_count = player_count
        self.starting_stack = starting_stack
        self.seed = seed
        self.random = random.Random(seed)
        self.min_visits = max(1, min_visits)
        self.smoothing_alpha = max(0.0, smoothing_alpha)
        self.base_table_path = base_table_path
        self.base_table = load_base_table(base_table_path)
        self.progress_callback = progress_callback
        self.progress_every = max(0, progress_every)
        self.preflop_only = preflop_only
        self.completed_iterations = 0
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
            traverser_seat = self.completed_iterations % self.player_count
            self.run_iteration(traverser_seat)
            completed = iteration_index + 1
            self.completed_iterations += 1
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
                        "completed_iterations_total": self.completed_iterations,
                        "total_iterations": self.iterations,
                        "percent": round((completed / max(1, self.iterations)) * 100, 2),
                        "message": (
                            f"Simulated {completed} / {self.iterations} "
                            f"{self.training_scope_label()} CFR iterations "
                            f"for {self.player_count} players "
                            f"(traverser seat {traverser_seat})."
                        ),
                        "elapsed_seconds": round(elapsed_seconds, 1),
                        "estimated_remaining_seconds": (
                            round(eta_seconds, 1) if eta_seconds is not None else None
                        ),
                        "infosets": len(self.visit_counts),
                    }
                )
        return self.average_strategy_table()

    def export_state(self) -> dict:
        return {
            "format": "multiway_preflop_mccfr_v1",
            "training_scope": "preflop" if self.preflop_only else "all_streets",
            "update_mode": UPDATE_MODE,
            "linear_cfr": True,
            "negative_regret_pruning": {
                "enabled": True,
                "probability": NEGATIVE_REGRET_PRUNE_PROBABILITY,
                "min_iterations": NEGATIVE_REGRET_PRUNE_MIN_ITERATIONS,
                "avg_gap_delta": NEGATIVE_REGRET_PRUNE_AVG_GAP_DELTA,
            },
            "learning_safety": {
                "enabled": LEARNING_SAFETY_ENABLED,
                "kind": "postflop_weak_action_strategy_and_utility_dampening",
            },
            "player_count": self.player_count,
            "starting_stack": self.starting_stack,
            "seed": self.seed,
            "min_visits": self.min_visits,
            "smoothing_alpha": self.smoothing_alpha,
            "base_table_path": self.base_table_path,
            "completed_iterations": self.completed_iterations,
            "regrets": {
                infoset: dict(sorted(weights.items()))
                for infoset, weights in sorted(self.regrets.items())
            },
            "strategy_sums": {
                infoset: dict(sorted(weights.items()))
                for infoset, weights in sorted(self.strategy_sums.items())
            },
            "visit_counts": dict(sorted(self.visit_counts.items())),
        }

    def load_state(self, state: dict) -> None:
        if state.get("format") != "multiway_preflop_mccfr_v1":
            raise ValueError("Unsupported checkpoint format.")
        if int(state.get("player_count", self.player_count)) != self.player_count:
            raise ValueError("Checkpoint player_count does not match current trainer.")
        if int(state.get("starting_stack", self.starting_stack)) != self.starting_stack:
            raise ValueError("Checkpoint starting_stack does not match current trainer.")

        self.min_visits = max(1, int(state.get("min_visits", self.min_visits)))
        self.smoothing_alpha = max(
            0.0, float(state.get("smoothing_alpha", self.smoothing_alpha))
        )
        self.seed = state.get("seed", self.seed)
        self.completed_iterations = max(
            0, int(state.get("completed_iterations", self.completed_iterations))
        )
        self.preflop_only = state.get("training_scope") == "preflop"
        self.regrets = defaultdict(lambda: defaultdict(float))
        self.strategy_sums = defaultdict(lambda: defaultdict(float))
        self.visit_counts = defaultdict(int)

        for infoset, weights in state.get("regrets", {}).items():
            self.regrets[infoset].update(
                {action: float(value) for action, value in weights.items()}
            )
        for infoset, weights in state.get("strategy_sums", {}).items():
            self.strategy_sums[infoset].update(
                {action: float(value) for action, value in weights.items()}
            )
        for infoset, count in state.get("visit_counts", {}).items():
            self.visit_counts[infoset] = int(count)

        checkpoint_base_table = state.get("base_table_path")
        if checkpoint_base_table and not self.base_table_path:
            self.base_table_path = checkpoint_base_table
            self.base_table = load_base_table(checkpoint_base_table)

    def run_iteration(self, traverser_seat: int) -> None:
        game = self.new_game()
        game.start_new_hand(autoplay_cpus=False)

        trajectory = []
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

            infoset = encode_infoset(game_state, player_state)
            strategy = (
                self.current_strategy(
                    infoset,
                    legal_actions,
                    game_state=game_state,
                    player_state=player_state,
                )
                if self.should_train_infoset(infoset)
                else self.rollout_strategy(game_state, player_state, legal_actions)
            )
            if seat == traverser_seat and self.should_train_infoset(infoset):
                self.visit_counts[infoset] += 1
                pruned_actions = self.pruned_traverser_actions(
                    infoset, self.abstract_actions(legal_actions)
                )
                sampling_strategy = self.without_pruned_actions(
                    strategy, pruned_actions, legal_actions
                )
                self.accumulate_average_strategy(
                    infoset,
                    legal_actions,
                    sampling_strategy,
                    weight=self.linear_iteration_weight(),
                )
                trajectory.append(
                    {
                        "game": deepcopy(game),
                        "seat": seat,
                        "legal_actions": deepcopy(legal_actions),
                        "infoset": infoset,
                        "strategy": dict(sampling_strategy),
                        "pruned_actions": sorted(pruned_actions),
                    }
                )
            else:
                sampling_strategy = strategy

            chosen_action = self.sample_action(sampling_strategy, legal_actions)
            payload = self.materialize_action(chosen_action, legal_actions)
            game.apply_player_action(seat, payload["type"], payload.get("amount"))

        for node in trajectory:
            self.update_regrets_for_node(
                node["game"],
                node["seat"],
                node["legal_actions"],
                node["infoset"],
                node["strategy"],
                set(node.get("pruned_actions", [])),
            )

    def evaluate_actions(
        self,
        game: SilentHoldemGame,
        seat: int,
        legal_actions: list[dict],
        *,
        action_names: list[str] | None = None,
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for action_name in action_names or self.abstract_actions(legal_actions):
            branch = deepcopy(game)
            payload = self.materialize_action(action_name, branch.legal_actions_for(seat))
            branch.apply_player_action(seat, payload["type"], payload.get("amount"))
            self.playout(branch)
            values[action_name] = self.utility_for(branch, seat)
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
            infoset = encode_infoset(game_state, player_state)
            if self.should_train_infoset(infoset):
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

    def update_regrets_for_node(
        self,
        game: SilentHoldemGame,
        seat: int,
        legal_actions: list[dict],
        infoset: str,
        strategy: dict[str, float],
        pruned_actions: set[str] | None = None,
    ) -> None:
        abstract_actions = self.abstract_actions(legal_actions)
        if not abstract_actions:
            return
        game_state = game.serialize_for_cpu()
        player_state = game.players[seat].to_public_dict(True)
        pruned_actions = pruned_actions or self.pruned_traverser_actions(
            infoset, abstract_actions
        )
        evaluated_actions = [
            action_name for action_name in abstract_actions if action_name not in pruned_actions
        ]
        action_utilities = self.evaluate_actions(
            game,
            seat,
            legal_actions,
            action_names=evaluated_actions,
        )
        action_utilities = self.apply_learning_safety_to_utilities(
            infoset, action_utilities, game_state, player_state
        )
        evaluated_strategy_total = sum(
            strategy.get(action_name, 0.0) for action_name in evaluated_actions
        )
        if evaluated_strategy_total > 0:
            pruned_utility = sum(
                strategy.get(action_name, 0.0) * action_utilities[action_name]
                for action_name in evaluated_actions
            ) / evaluated_strategy_total
        else:
            pruned_utility = 0.0
        for action_name in pruned_actions:
            action_utilities[action_name] = pruned_utility
        node_utility = sum(
            strategy.get(action_name, 0.0) * action_utilities[action_name]
            for action_name in abstract_actions
        )
        regret_weight = self.linear_iteration_weight()
        for action_name in abstract_actions:
            self.regrets[infoset][action_name] += (
                action_utilities[action_name] - node_utility
            ) * regret_weight

    def pruned_traverser_actions(self, infoset: str, actions: list[str]) -> set[str]:
        if self.completed_iterations < NEGATIVE_REGRET_PRUNE_MIN_ITERATIONS:
            return set()
        if self.random.random() >= NEGATIVE_REGRET_PRUNE_PROBABILITY:
            return set()
        regrets = self.regrets.get(infoset, {})
        visit_count = max(1, self.visit_counts.get(infoset, 0))
        avg_regrets = {
            action: regrets.get(action, 0.0) / visit_count for action in actions
        }
        best = max(avg_regrets.values())
        pruned = {
            action
            for action in actions
            if avg_regrets[action] - best < -NEGATIVE_REGRET_PRUNE_AVG_GAP_DELTA
        }
        if len(pruned) >= len(actions):
            best_action = max(actions, key=lambda action: avg_regrets[action])
            pruned.discard(best_action)
        return pruned

    def without_pruned_actions(
        self,
        strategy: dict[str, float],
        pruned_actions: set[str],
        legal_actions: list[dict],
    ) -> dict[str, float]:
        if not pruned_actions:
            return normalize_weights(dict(strategy))
        weights = {
            action_name: max(0.0, strategy.get(action_name, 0.0))
            for action_name in self.abstract_actions(legal_actions)
            if action_name not in pruned_actions
        }
        if not weights:
            return normalize_weights(dict(strategy))
        return normalize_weights(weights)

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
            action_name: max(0.0, self.regrets[infoset].get(action_name, 0.0))
            for action_name in self.abstract_actions(legal_actions)
        }
        total = sum(positive.values())
        if total > 0:
            regret_strategy = {
                action_type: value / total for action_type, value in positive.items()
            }
            visits = self.visit_counts.get(infoset, 0)
            table_weight = min(0.9, 0.42 + visits / 180.0)
            return self.apply_learning_safety_to_strategy(
                infoset,
                self.complete_legal_strategy(
                    blend_with_blueprint(
                        self.to_base_action_strategy(regret_strategy),
                        infoset,
                        legal_actions,
                        table_weight=table_weight,
                        game_state=game_state,
                        player_state=player_state,
                    ),
                    legal_actions,
                ),
            )

        if self.visit_counts.get(infoset, 0) <= 1:
            return self.apply_learning_safety_to_strategy(
                infoset, self.grouped_initial_strategy(legal_actions)
            )

        base_strategy = self.lookup_base_strategy(infoset, legal_actions)
        if base_strategy:
            visits = self.visit_counts.get(infoset, 0)
            table_weight = min(0.94, 0.76 + visits / 260.0)
            expanded_base = self.expand_base_strategy(base_strategy, legal_actions)
            blended_base = blend_with_blueprint(
                base_strategy,
                infoset,
                legal_actions,
                table_weight=table_weight,
                game_state=game_state,
                player_state=player_state,
            )
            blended_expanded = self.expand_base_strategy(blended_base, legal_actions)
            return self.apply_learning_safety_to_strategy(
                infoset,
                self.complete_abstract_strategy(
                    self.mix_strategies(expanded_base, blended_expanded, first_weight=0.65),
                    legal_actions,
                ),
            )

        blueprint = build_preflop_blueprint(
            infoset,
            legal_actions,
            game_state=game_state,
            player_state=player_state,
        )
        if blueprint:
            return self.apply_learning_safety_to_strategy(
                infoset,
                self.complete_abstract_strategy(
                    self.expand_base_strategy(blueprint, legal_actions),
                    legal_actions,
                ),
            )
        return self.apply_learning_safety_to_strategy(infoset, self.uniform_strategy(legal_actions))

    def rollout_strategy(
        self, game_state: dict, player_state: dict, legal_actions: list[dict]
    ) -> dict[str, float]:
        infoset = encode_infoset(game_state, player_state)
        base_strategy = self.lookup_base_strategy(infoset, legal_actions)
        if base_strategy:
            return self.apply_learning_safety_to_strategy(
                infoset,
                self.complete_abstract_strategy(
                    self.expand_base_strategy(
                        runtime_table_cpu.apply_safety_overrides(base_strategy, infoset, legal_actions),
                        legal_actions,
                    ),
                    legal_actions,
                ),
            )
        return self.apply_learning_safety_to_strategy(infoset, self.passive_fallback_strategy(legal_actions))

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
        for action_name in self.abstract_actions(legal_actions):
            action_type = self.base_action_name(action_name)
            if action_type in {"check", "call"}:
                weights[action_name] = 4.0
            elif action_type == "fold":
                weights[action_name] = 2.4
            elif action_type in {"bet", "raise"}:
                weights[action_name] = 0.7
            elif action_type == "all-in":
                weights[action_name] = 0.08
            else:
                weights[action_name] = 1.0
        return normalize_weights(weights)

    def uniform_strategy(self, legal_actions: list[dict]) -> dict[str, float]:
        actions = self.abstract_actions(legal_actions)
        weight = 1.0 / len(actions)
        return {action_name: weight for action_name in actions}

    def grouped_initial_strategy(self, legal_actions: list[dict]) -> dict[str, float]:
        groups: dict[str, list[str]] = {}
        for action_name in self.abstract_actions(legal_actions):
            group = self.initial_action_group(action_name, legal_actions)
            groups.setdefault(group, []).append(action_name)
        if not groups:
            return {}

        group_weight = 1.0 / len(groups)
        strategy = {}
        for actions in groups.values():
            action_weight = group_weight / len(actions)
            for action_name in actions:
                strategy[action_name] = action_weight
        return normalize_weights(strategy)

    def initial_action_group(self, action_name: str, legal_actions: list[dict]) -> str:
        base_action = self.base_action_name(action_name)
        if base_action != "all-in":
            return base_action
        legal_bases = {action.get("type") for action in legal_actions}
        if "raise" in legal_bases:
            return "raise"
        if "bet" in legal_bases:
            return "bet"
        return "all-in"

    def complete_legal_strategy(
        self, strategy: dict[str, float], legal_actions: list[dict]
    ) -> dict[str, float]:
        return self.complete_abstract_strategy(
            self.expand_base_strategy(strategy, legal_actions),
            legal_actions,
        )

    def complete_abstract_strategy(
        self, strategy: dict[str, float], legal_actions: list[dict]
    ) -> dict[str, float]:
        weights = {}
        for action_name in self.abstract_actions(legal_actions):
            weights[action_name] = max(0.001, float(strategy.get(action_name, 0.0)))
        return normalize_weights(weights)

    def apply_learning_safety_to_strategy(
        self, infoset: str, strategy: dict[str, float]
    ) -> dict[str, float]:
        if not LEARNING_SAFETY_ENABLED:
            return normalize_weights(dict(strategy))
        adjusted = {
            action: probability * self.learning_safety_strategy_multiplier(infoset, action)
            for action, probability in strategy.items()
        }
        return normalize_weights(adjusted)

    def learning_safety_strategy_multiplier(self, infoset: str, action: str) -> float:
        parts = infoset.split("|")
        if len(parts) != 7:
            return 1.0
        phase, player_count, _position, bucket, pressure, _stack_bucket, texture = parts
        if phase == "preflop":
            return 1.0
        base_action = self.base_action_name(action)
        player_total = (
            int(player_count[:-1])
            if player_count.endswith("p") and player_count[:-1].isdigit()
            else self.player_count
        )

        if base_action == "call" and bucket == "air" and pressure == "jam":
            if texture == "dry":
                return 0.10
            if texture in {"connected", "two_tone"}:
                return 0.18
            return 0.25
        if base_action == "call" and bucket == "marginal" and pressure == "jam" and texture == "dry":
            return 0.45
        if base_action not in {"bet", "raise", "all-in"}:
            return 1.0

        if bucket == "air" and pressure == "jam":
            if base_action == "all-in":
                return 0.003
            if action.endswith("_large"):
                return 0.01
            if action.endswith("_medium"):
                return 0.025
            if action.endswith("_small"):
                return 0.06
        if bucket == "air" and pressure == "large":
            if base_action == "all-in":
                return 0.01
            if action.endswith("_large"):
                return 0.06
        if bucket == "marginal" and pressure == "jam":
            if base_action == "all-in":
                return 0.025
            if action.endswith("_large"):
                return 0.12
        if bucket == "draw" and pressure == "jam" and texture not in {"very_wet", "connected", "two_tone"}:
            if base_action == "all-in":
                return 0.08
        if player_total >= 4 and bucket not in {"monster", "made", "strong_pair", "combo_draw"}:
            if base_action == "all-in":
                return 0.12
        return 1.0

    def abstract_actions(self, legal_actions: list[dict]) -> list[str]:
        actions = []
        strategy_all_in_allowed = self.all_in_as_strategy_action_allowed(legal_actions)
        for action in legal_actions:
            action_type = action["type"]
            if action_type in {"bet", "raise"}:
                sizes = action.get("abstract_sizes") or []
                size_names = [
                    size["name"]
                    for size in sizes
                    if size.get("name") in SIZE_SUFFIXES
                ]
                if not size_names:
                    size_names = ["small"]
                for size_name in size_names:
                    actions.append(f"{action_type}_{size_name}")
            elif action_type == "all-in" and not strategy_all_in_allowed:
                continue
            else:
                actions.append(action_type)
        return actions

    def all_in_as_strategy_action_allowed(self, legal_actions: list[dict]) -> bool:
        for action in legal_actions:
            if action.get("type") not in {"bet", "raise"}:
                continue
            max_total = int(action.get("max_total", 0) or 0)
            large = next(
                (
                    size
                    for size in action.get("abstract_sizes", [])
                    if size.get("name") == "large"
                ),
                None,
            )
            if large is None or int(large.get("total", 0) or 0) >= max_total:
                return True
        return False

    def apply_learning_safety_to_utilities(
        self,
        infoset: str,
        utilities: dict[str, float],
        game_state: dict,
        player_state: dict,
    ) -> dict[str, float]:
        if not LEARNING_SAFETY_ENABLED:
            return dict(utilities)
        if not utilities:
            return {}

        passive_values = [
            value
            for action, value in utilities.items()
            if self.base_action_name(action) in {"fold", "check", "call"}
        ]
        fold_check_values = [
            value
            for action, value in utilities.items()
            if self.base_action_name(action) in {"fold", "check"}
        ]
        safe_baseline = max(passive_values) if passive_values else max(utilities.values())
        fold_check_baseline = (
            max(fold_check_values) if fold_check_values else safe_baseline
        )
        penalty_unit = max(1.0, float(game_state.get("big_blind", self.starting_stack / 100)))

        adjusted = dict(utilities)
        for action, value in utilities.items():
            cap = self.learning_safety_utility_cap(
                infoset,
                action,
                safe_baseline,
                fold_check_baseline,
                penalty_unit,
            )
            if cap is not None:
                adjusted[action] = min(value, cap)
        return adjusted

    def learning_safety_utility_cap(
        self,
        infoset: str,
        action: str,
        safe_baseline: float,
        fold_check_baseline: float,
        penalty_unit: float,
    ) -> float | None:
        parts = infoset.split("|")
        if len(parts) != 7:
            return None
        phase, player_count, _position, bucket, pressure, _stack_bucket, texture = parts
        if phase == "preflop":
            return None

        base_action = self.base_action_name(action)
        player_total = (
            int(player_count[:-1])
            if player_count.endswith("p") and player_count[:-1].isdigit()
            else self.player_count
        )
        weak_bucket = bucket == "air"
        marginal_bucket = bucket == "marginal"
        draw_bucket = bucket in {"draw", "combo_draw"}
        wet_board = texture in {"very_wet", "connected", "two_tone", "paired"}

        if weak_bucket and pressure == "jam" and base_action == "call":
            if texture == "dry":
                return fold_check_baseline - penalty_unit * 0.25
            if texture in {"connected", "two_tone"}:
                return fold_check_baseline + penalty_unit * 0.10
            return fold_check_baseline + penalty_unit * 0.20
        if marginal_bucket and pressure == "jam" and texture == "dry" and base_action == "call":
            return fold_check_baseline + penalty_unit * 0.25
        if base_action not in {"bet", "raise", "all-in"}:
            return None

        if weak_bucket and pressure == "jam":
            if base_action == "all-in":
                return safe_baseline - penalty_unit * 2.8
            if action.endswith("_large"):
                return safe_baseline - penalty_unit * 2.2
            if action.endswith("_medium"):
                return safe_baseline - penalty_unit * 1.6
            if action.endswith("_small"):
                return safe_baseline - penalty_unit * 1.1
        if weak_bucket and pressure == "large":
            if base_action == "all-in":
                return safe_baseline - penalty_unit * 2.0
            if action.endswith("_large"):
                return safe_baseline - penalty_unit * 1.4
        if marginal_bucket and pressure == "jam":
            if base_action == "all-in":
                return safe_baseline - penalty_unit * 1.4
            if action.endswith("_large"):
                return safe_baseline - penalty_unit * 0.8
        if draw_bucket and pressure == "jam" and not wet_board:
            if base_action == "all-in":
                return safe_baseline - penalty_unit * 0.9
        if player_total >= 4 and bucket not in {"monster", "made", "strong_pair", "combo_draw"}:
            if base_action == "all-in":
                return safe_baseline - penalty_unit * 0.9
        return None

    def to_base_action_strategy(self, strategy: dict[str, float]) -> dict[str, float]:
        base = {}
        for action_name, probability in strategy.items():
            base_action = self.base_action_name(action_name)
            base[base_action] = base.get(base_action, 0.0) + probability
        return normalize_weights(base)

    def expand_base_strategy(
        self, strategy: dict[str, float], legal_actions: list[dict]
    ) -> dict[str, float]:
        expanded = {}
        for action in legal_actions:
            action_type = action["type"]
            probability = float(strategy.get(action_type, 0.0))
            if action_type in {"bet", "raise"}:
                size_actions = [
                    action_name
                    for action_name in self.abstract_actions([action])
                    if action_name.startswith(f"{action_type}_")
                ]
                size_weights = self.default_size_weights(action_type, size_actions)
                for action_name in size_actions:
                    expanded[action_name] = probability * size_weights.get(action_name, 0.0)
            else:
                expanded[action_type] = probability
        return normalize_weights(expanded) if expanded else {}

    def default_size_weights(self, action_type: str, size_actions: list[str]) -> dict[str, float]:
        profile = {
            f"{action_type}_small": 0.42,
            f"{action_type}_medium": 0.36,
            f"{action_type}_large": 0.22,
        }
        weights = {action_name: profile.get(action_name, 1.0) for action_name in size_actions}
        return normalize_weights(weights)

    def mix_strategies(
        self, first: dict[str, float], second: dict[str, float], *, first_weight: float
    ) -> dict[str, float]:
        keys = set(first) | set(second)
        mixed = {
            key: first.get(key, 0.0) * first_weight + second.get(key, 0.0) * (1.0 - first_weight)
            for key in keys
        }
        return normalize_weights(mixed)

    def should_train_infoset(self, infoset: str) -> bool:
        return not self.preflop_only or infoset.startswith("preflop|")

    def training_scope_label(self) -> str:
        return "preflop-only" if self.preflop_only else "all-street"

    def linear_iteration_weight(self) -> float:
        return float(max(1, self.completed_iterations + 1))

    def accumulate_average_strategy(
        self,
        infoset: str,
        legal_actions: list[dict],
        strategy: dict[str, float],
        *,
        weight: float = 1.0,
    ) -> None:
        for action in legal_actions:
            for action_name in self.abstract_actions([action]):
                self.strategy_sums[infoset][action_name] += strategy.get(action_name, 0.0) * weight

    def sample_action(self, strategy: dict[str, float], legal_actions: list[dict]) -> str:
        threshold = self.random.random()
        cumulative = 0.0
        abstract_actions = self.abstract_actions(legal_actions)
        for action_type in abstract_actions:
            cumulative += strategy.get(action_type, 0.0)
            if threshold <= cumulative:
                return action_type
        return abstract_actions[-1]

    def materialize_action(self, action_type: str, legal_actions: list[dict]) -> dict:
        base_action, size_name = self.split_action_name(action_type)
        for action in legal_actions:
            if action["type"] != base_action:
                continue
            payload = {"type": base_action}
            if base_action in {"bet", "raise"}:
                payload["amount"] = self.amount_for_size(action, size_name)
            elif "amount" in action:
                payload["amount"] = action["amount"]
            return payload
        fallback = legal_actions[0]
        return {"type": fallback["type"], "amount": fallback.get("amount")}

    def split_action_name(self, action_name: str) -> tuple[str, str | None]:
        for base_action in ("bet", "raise"):
            prefix = f"{base_action}_"
            if action_name.startswith(prefix):
                return base_action, action_name.removeprefix(prefix)
        return action_name, None

    def base_action_name(self, action_name: str) -> str:
        return self.split_action_name(action_name)[0]

    def amount_for_size(self, action: dict, size_name: str | None) -> int:
        sizes = action.get("abstract_sizes") or []
        for size in sizes:
            if size.get("name") == size_name:
                return int(size["total"])
        return int(action["min_total"])

    def strategy_prior(
        self,
        infoset: str,
        actions: list[str],
    ) -> dict[str, float]:
        base_actions = sorted({self.base_action_name(action) for action in actions})
        blueprint = build_preflop_blueprint(
            infoset,
            [{"type": action} for action in base_actions],
        )
        if blueprint:
            expanded = {}
            for action in actions:
                base_action = self.base_action_name(action)
                expanded[action] = max(0.05, blueprint.get(base_action, 0.0) * 14.0)
            return expanded

        phase, player_count, position, bucket, pressure, stack_bucket, texture = infoset.split("|")
        player_total = (
            int(player_count[:-1])
            if player_count.endswith("p") and player_count[:-1].isdigit()
            else self.player_count
        )
        color_index = COLOR_TO_INDEX.get(bucket)
        weak_bucket = bucket in {"weak", "air", "ash"} or color_index is not None and color_index <= COLOR_TO_INDEX["pink"]
        speculative_bucket = bucket in {"speculative", "draw", "combo_draw", "purple", "white"}
        marginal_bucket = bucket in {"medium", "marginal", "blue", "green"}
        strong_bucket = (
            bucket in {"strong", "premium", "strong_pair", "made", "monster", "yellow", "red", "navy"}
            or color_index is not None and color_index >= COLOR_TO_INDEX["yellow"]
        )
        jammed = pressure == "jam"

        prior = {action: 1.0 for action in actions}
        for action in actions:
            base_action = self.base_action_name(action)
            if base_action in {"check", "call"}:
                prior[action] = 3.2 if weak_bucket else 2.4 if marginal_bucket else 2.0
            elif base_action == "fold":
                prior[action] = 2.8 if weak_bucket and not jammed else 1.5 if marginal_bucket else 1.0
            elif base_action in {"bet", "raise"}:
                if weak_bucket:
                    prior[action] = 0.42 if phase == "river" else 0.28
                elif speculative_bucket:
                    prior[action] = 1.05 if texture in {"very_wet", "connected", "two_tone"} else 0.82
                elif marginal_bucket:
                    prior[action] = 0.72
                elif strong_bucket:
                    prior[action] = 2.0 if bucket == "monster" else 1.7
                if action.endswith("_small"):
                    prior[action] *= 1.12 if not strong_bucket else 0.85
                elif action.endswith("_medium"):
                    prior[action] *= 1.0
                elif action.endswith("_large"):
                    prior[action] *= 0.72 if not strong_bucket else 1.18
            elif base_action == "all-in":
                if weak_bucket and not jammed:
                    prior[action] = 0.03
                elif bucket == "monster":
                    prior[action] = 1.0 if stack_bucket in {"shallow", "medium"} else 0.28
                elif stack_bucket == "shallow":
                    prior[action] = 0.7 if strong_bucket else 0.2
                else:
                    prior[action] = 0.08 if player_total >= 4 else 0.16

        if phase == "preflop" and position in {"early", "blind", "big_blind"} and weak_bucket:
            for action in list(prior):
                if action.startswith("raise_"):
                    prior[action] *= 0.7
        if phase != "preflop" and player_total >= 4:
            for action in list(prior):
                if self.base_action_name(action) in {"bet", "raise", "all-in"} and not strong_bucket:
                    prior[action] *= 0.75
        if player_total >= 5 and "all-in" in prior and not jammed:
            prior["all-in"] *= 0.7
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
        blend_weight = min(0.92, max(0.5, visits / 180.0))
        base_blend = blend_with_blueprint(
            self.to_base_action_strategy(normalized),
            infoset,
            [{"type": action} for action in sorted({self.base_action_name(action) for action in actions})],
            table_weight=blend_weight,
        )
        blueprint_expanded = {}
        for action in actions:
            base_action = self.base_action_name(action)
            if base_action in {"bet", "raise"}:
                siblings = [candidate for candidate in actions if self.base_action_name(candidate) == base_action]
                size_weights = self.default_size_weights(base_action, siblings)
                blueprint_expanded[action] = base_blend.get(base_action, 0.0) * size_weights.get(action, 0.0)
            else:
                blueprint_expanded[action] = base_blend.get(base_action, 0.0)
        return self.mix_strategies(normalized, blueprint_expanded, first_weight=blend_weight)

    def average_strategy_table(self) -> dict[str, dict[str, float]]:
        table: dict[str, dict[str, float]] = {}
        for infoset, weights in self.strategy_sums.items():
            if not self.should_train_infoset(infoset):
                continue
            visits = self.visit_counts.get(infoset, 0)
            if visits < self.min_visits:
                continue
            smoothed = self.apply_learning_safety_to_strategy(
                infoset, self.smooth_strategy(infoset, dict(weights), visits)
            )
            table[infoset] = {
                action: round(value, 6) for action, value in sorted(smoothed.items())
            }
        return dict(sorted(table.items()))

    def pruned_visit_counts(self) -> dict[str, int]:
        return {
            infoset: count
            for infoset, count in sorted(self.visit_counts.items())
            if self.should_train_infoset(infoset) and count >= self.min_visits
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a multiway all-street strategy table with approximate MCCFR."
    )
    parser.add_argument("--iterations", type=int, default=2000, help="Training iterations")
    parser.add_argument("--players", type=int, default=6, help="Total player count (2-9)")
    parser.add_argument("--stack", type=int, default=5000, help="Starting stack")
    parser.add_argument(
        "--out",
        default=str(
            BASE_DIR / "app" / "sample_cpus" / "strategy_tables" / "multiway_mccfr.json"
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
    parser.add_argument(
        "--resume-state",
        default=None,
        help="Resume exact learning state from a previously saved checkpoint JSON.",
    )
    parser.add_argument(
        "--checkpoint-out",
        default=None,
        help="Where to save the full MCCFR checkpoint state JSON.",
    )
    parser.add_argument(
        "--preflop-only",
        action="store_true",
        help="Keep the previous behavior and update/export only preflop infosets.",
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
        preflop_only=args.preflop_only,
    )
    if args.resume_state:
        checkpoint = json.loads(
            Path(args.resume_state).expanduser().resolve().read_text(encoding="utf-8")
        )
        trainer.load_state(checkpoint)

    if args.print_every > 0:
        checkpoint_path = resolve_checkpoint_path(args.checkpoint_out, args.out)
        for chunk_start in range(0, args.iterations, args.print_every):
            chunk = min(args.print_every, args.iterations - chunk_start)
            trainer.iterations = chunk
            trainer.train()
            trainer.iterations = args.iterations
            done = chunk_start + chunk
            write_checkpoint(trainer, checkpoint_path)
            write_strategy_outputs(trainer, args.out)
            print(
                f"[multiway-mccfr] {done}/{args.iterations} iterations completed",
                file=sys.stderr,
            )
    else:
        trainer.train()

    table, visit_counts, out_path, visits_path = write_strategy_outputs(trainer, args.out)

    checkpoint_path = resolve_checkpoint_path(args.checkpoint_out, args.out)
    write_checkpoint(trainer, checkpoint_path)

    summary = {
        "iterations": args.iterations,
        "completed_iterations_total": trainer.completed_iterations,
        "players": args.players,
        "starting_stack": args.stack,
        "infosets": len(table),
        "visit_entries": len(visit_counts),
        "min_visits": trainer.min_visits,
        "smoothing_alpha": trainer.smoothing_alpha,
        "base_table": trainer.base_table_path,
        "resume_state": args.resume_state,
        "output": str(out_path),
        "visits_output": str(visits_path),
        "checkpoint_output": str(checkpoint_path),
        "training_scope": "preflop" if trainer.preflop_only else "all_streets",
        "update_mode": UPDATE_MODE,
        "linear_cfr": True,
        "negative_regret_pruning": {
            "enabled": True,
            "probability": NEGATIVE_REGRET_PRUNE_PROBABILITY,
            "min_iterations": NEGATIVE_REGRET_PRUNE_MIN_ITERATIONS,
            "avg_gap_delta": NEGATIVE_REGRET_PRUNE_AVG_GAP_DELTA,
        },
        "learning_safety": {
            "enabled": LEARNING_SAFETY_ENABLED,
            "kind": "postflop_weak_action_strategy_and_utility_dampening",
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def resolve_checkpoint_path(checkpoint_out: str | None, out_path: str) -> Path:
    destination = Path(out_path).expanduser().resolve()
    return (
        Path(checkpoint_out).expanduser().resolve()
        if checkpoint_out
        else destination.with_name(f"{destination.stem}_state.json")
    )


def write_checkpoint(trainer: MultiwayPreflopMccfrTrainer, checkpoint_path: Path) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(trainer.export_state(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_strategy_outputs(
    trainer: MultiwayPreflopMccfrTrainer, out_path: str
) -> tuple[dict[str, dict[str, float]], dict[str, int], Path, Path]:
    table = trainer.average_strategy_table()
    visit_counts = trainer.pruned_visit_counts()
    destination = Path(out_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    visits_path = destination.with_name(f"{destination.stem}_visits.json")
    visits_path.write_text(json.dumps(visit_counts, ensure_ascii=False, indent=2), encoding="utf-8")
    return table, visit_counts, destination, visits_path


def build_and_save_multiway_strategy_table(
    *,
    iterations: int,
    player_count: int,
    starting_stack: int,
    out_path: str,
    seed: int = 11,
    min_visits: int = 40,
    smoothing_alpha: float = 10.0,
    base_table_path: str | None = None,
    resume_state_path: str | None = None,
    checkpoint_out_path: str | None = None,
    progress_callback=None,
    progress_every: int = 200,
    preflop_only: bool = False,
) -> dict:
    trainer = MultiwayPreflopMccfrTrainer(
        iterations=iterations,
        player_count=player_count,
        starting_stack=starting_stack,
        seed=seed,
        min_visits=min_visits,
        smoothing_alpha=smoothing_alpha,
        base_table_path=base_table_path,
        progress_callback=progress_callback,
        progress_every=progress_every,
        preflop_only=preflop_only,
    )
    if resume_state_path:
        checkpoint = json.loads(
            Path(resume_state_path).expanduser().resolve().read_text(encoding="utf-8")
        )
        trainer.load_state(checkpoint)
    started_at = time.perf_counter()
    table = trainer.train()
    visit_counts = trainer.pruned_visit_counts()

    destination = Path(out_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")

    visits_path = destination.with_name(f"{destination.stem}_visits.json")
    visits_path.write_text(json.dumps(visit_counts, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint_path = (
        Path(checkpoint_out_path).expanduser().resolve()
        if checkpoint_out_path
        else destination.with_name(f"{destination.stem}_state.json")
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps(trainer.export_state(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "iterations": iterations,
        "completed_iterations_total": trainer.completed_iterations,
        "players": player_count,
        "starting_stack": starting_stack,
        "infosets": len(table),
        "visit_entries": len(visit_counts),
        "min_visits": trainer.min_visits,
        "smoothing_alpha": trainer.smoothing_alpha,
        "base_table": trainer.base_table_path,
        "resume_state": resume_state_path,
        "output": str(destination),
        "visits_output": str(visits_path),
        "checkpoint_output": str(checkpoint_path),
        "training_scope": "preflop" if trainer.preflop_only else "all_streets",
        "update_mode": UPDATE_MODE,
        "linear_cfr": True,
        "negative_regret_pruning": {
            "enabled": True,
            "probability": NEGATIVE_REGRET_PRUNE_PROBABILITY,
            "min_iterations": NEGATIVE_REGRET_PRUNE_MIN_ITERATIONS,
            "avg_gap_delta": NEGATIVE_REGRET_PRUNE_AVG_GAP_DELTA,
        },
        "learning_safety": {
            "enabled": LEARNING_SAFETY_ENABLED,
            "kind": "postflop_weak_action_strategy_and_utility_dampening",
        },
        "elapsed_seconds": round(max(0.0, time.perf_counter() - started_at), 1),
        "strategy_table": table,
        "strategy_table_filename": destination.name,
    }


if __name__ == "__main__":
    main()
