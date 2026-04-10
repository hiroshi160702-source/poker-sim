from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path

from app.strategy_tables.lib import candidate_infosets, encode_infoset

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TABLE_CANDIDATES = [
    Path(__file__).resolve().parent / "strategy_tables" / "example_gto.json",
    APP_ROOT / "sample_cpus" / "strategy_tables" / "multiway_3p_100000.json",
    APP_ROOT / "sample_cpus" / "strategy_tables" / "table_builder_expanded_200000.json",
    APP_ROOT / "sample_cpus" / "strategy_tables" / "example_gto.json",
]


def decide_action(game_state, player_state, legal_actions):
    table = load_strategy_table()
    infoset = encode_infoset(game_state, player_state)
    strategy = lookup_strategy(table, infoset, legal_actions)
    action_type = sample_action(strategy)
    return materialize_action(action_type, legal_actions, infoset)


@lru_cache(maxsize=4)
def load_strategy_table(table_path: str | None = None):
    path = resolve_table_path(table_path)
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_table_path(table_path: str | None) -> Path:
    if table_path:
        path = Path(table_path).resolve()
        if path.exists():
            return path
    for candidate in DEFAULT_TABLE_CANDIDATES:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("No strategy table JSON was found for strategy_table_cpu.py.")


def lookup_strategy(table, infoset, legal_actions):
    legal_types = {action["type"] for action in legal_actions}

    for key in candidate_infosets(infoset):
        strategy = table.get(key)
        if strategy:
            filtered = {action: prob for action, prob in strategy.items() if action in legal_types}
            if filtered:
                return normalize(filtered)

    fallback = {}
    for action in legal_actions:
        action_type = action["type"]
        if action_type in {"check", "call"}:
            fallback[action_type] = 3.0
        elif action_type in {"bet", "raise"}:
            fallback[action_type] = 2.0
        else:
            fallback[action_type] = 1.0
    return normalize(fallback)


def normalize(weights):
    total = sum(weights.values())
    if total <= 0:
        uniform = 1.0 / len(weights)
        return {action: uniform for action in weights}
    return {action: value / total for action, value in weights.items()}


def sample_action(strategy):
    threshold = random.random()
    cumulative = 0.0
    items = list(strategy.items())
    for action, probability in items:
        cumulative += probability
        if threshold <= cumulative:
            return action
    return items[-1][0]


def materialize_action(action_type, legal_actions, infoset):
    for action in legal_actions:
        if action["type"] != action_type:
            continue

        payload = {"type": action_type}
        if action_type in {"bet", "raise"}:
            payload["amount"] = choose_size(action, infoset)
        elif "amount" in action:
            payload["amount"] = action["amount"]
        return payload

    first = legal_actions[0]
    return {"type": first["type"], "amount": first.get("amount")}


def choose_size(action, infoset):
    min_total = action["min_total"]
    max_total = action["max_total"]
    if max_total <= min_total:
        return max_total

    phase, _position, bucket, pressure, stack_bucket, _texture = infoset.split("|")
    span = max_total - min_total
    if phase == "preflop":
        if bucket in {"premium", "strong"}:
            factor = 0.48 if pressure == "none" else 0.36
        elif bucket == "speculative":
            factor = 0.22
        else:
            factor = 0.15
    else:
        if bucket == "monster":
            factor = 0.68
        elif bucket == "made":
            factor = 0.55
        elif bucket in {"draw", "combo_draw"}:
            factor = 0.32
        elif bucket == "strong_pair":
            factor = 0.26
        else:
            factor = 0.18

    if stack_bucket == "shallow":
        factor = min(0.82, factor + 0.14)
    elif stack_bucket == "very_deep":
        factor = max(0.12, factor - 0.05)

    return max(min_total, min(max_total, min_total + int(span * factor)))
