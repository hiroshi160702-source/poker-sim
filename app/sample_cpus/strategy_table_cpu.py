from __future__ import annotations

"""JSON の戦略表を参照して行動を選ぶ CPU です。"""

import json
import random
from functools import lru_cache
from pathlib import Path

import app as app_package
from app.strategy_tables.lib import candidate_infosets, encode_infoset
from app.strategy_tables.preflop_blueprint import blend_with_blueprint

PACKAGE_ROOT = Path(app_package.__file__).resolve().parent
DEFAULT_TABLE_CANDIDATES = [
    Path(__file__).resolve().parent / "strategy_tables" / "multiplayer_strategy_6p_5000000hands.json",
    PACKAGE_ROOT / "sample_cpus" / "strategy_tables" / "multiway_3p_100000.json",
    PACKAGE_ROOT / "sample_cpus" / "strategy_tables" / "table_builder_expanded_200000.json",
    PACKAGE_ROOT / "sample_cpus" / "strategy_tables" / "example_gto.json",
]


def decide_action(game_state, player_state, legal_actions):
    # 戦略表は infoset をキーにしているため、対局中は参照して
    # 確率的にアクションを選ぶだけで動きます。
    table = load_strategy_table()
    infoset = encode_infoset(game_state, player_state)
    strategy = lookup_strategy(table, infoset, legal_actions)
    strategy = blend_with_blueprint(
        strategy,
        infoset,
        legal_actions,
        table_weight=0.82,
        game_state=game_state,
        player_state=player_state,
    )
    strategy = apply_safety_overrides(strategy, infoset, legal_actions)
    action_type = sample_action(strategy)
    return materialize_action(action_type, legal_actions, infoset)


@lru_cache(maxsize=4)
def load_strategy_table(table_path: str | None = None):
    path = resolve_table_path(table_path)
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_table_path(table_path: str | None) -> Path:
    # アップロード CPU は app/sample_cpus の外で動くことがあるため、
    # ローカル配置先とパッケージ内の両方を探索します。
    if table_path:
        path = Path(table_path).resolve()
        if path.exists():
            return path
    sibling_jsons = sorted(Path(__file__).resolve().parent.glob("*.json"))
    if sibling_jsons:
        return sibling_jsons[0].resolve()
    for candidate in DEFAULT_TABLE_CANDIDATES:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("No strategy table JSON was found for strategy_table_cpu.py.")


def lookup_strategy(table, infoset, legal_actions):
    # 厳密な infoset から広い "any" バケットへ順に探し、現在局面に合う
    # 合法アクション分布を見つけます。
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


def apply_safety_overrides(strategy, infoset, legal_actions):
    # 自己対戦由来の疎な戦略表には、弱いハンドでも raise/all-in に
    # 偏った行が残ることがあります。実戦側では最低限の安全弁を入れて、
    # 明らかに不自然なオールイン頻度を抑えます。
    weights = dict(strategy)
    legal_types = {action["type"] for action in legal_actions}
    if "all-in" not in legal_types and "raise" not in legal_types and "bet" not in legal_types:
        return normalize(weights)

    phase, player_count, _position, bucket, pressure, stack_bucket, _texture = infoset.split("|")
    player_total = int(player_count[:-1]) if player_count.endswith("p") and player_count[:-1].isdigit() else 2
    weak_bucket = bucket in {"weak", "air", "marginal"}
    medium_bucket = bucket in {"medium", "draw", "speculative"}
    already_committed = pressure == "jam"
    multiway = player_total >= 3

    if "all-in" in weights:
        if weak_bucket and not already_committed:
            weights["all-in"] *= 0.05
        elif medium_bucket and not already_committed:
            weights["all-in"] *= 0.18
        elif phase != "preflop" and stack_bucket != "shallow" and not already_committed:
            weights["all-in"] *= 0.35
        if multiway and not already_committed:
            weights["all-in"] *= 0.45

    if weak_bucket and pressure in {"none", "tiny", "small"}:
        if "raise" in weights:
            weights["raise"] *= 0.35
        if "bet" in weights:
            weights["bet"] *= 0.35
    elif medium_bucket and pressure in {"none", "tiny"}:
        if "raise" in weights:
            weights["raise"] *= 0.7
        if "bet" in weights:
            weights["bet"] *= 0.7

    passive_boost = 1.0
    if weak_bucket:
        passive_boost = 1.6 if pressure in {"none", "tiny", "small"} else 1.25
    elif medium_bucket and pressure in {"none", "tiny"}:
        passive_boost = 1.2

    for action_type in ("check", "call", "fold"):
        if action_type in weights:
            weights[action_type] *= passive_boost

    return normalize(weights)


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
    # 戦略表には行動確率しかないため、ベットサイズは同じ infoset を使って
    # ヒューリスティックに決めています。
    min_total = action["min_total"]
    max_total = action["max_total"]
    if max_total <= min_total:
        return max_total

    phase, _player_count, _position, bucket, pressure, stack_bucket, _texture = infoset.split("|")
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
