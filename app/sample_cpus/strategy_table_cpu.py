from __future__ import annotations

"""JSON の戦略表を参照して行動を選ぶ CPU です。"""

import json
import random
from functools import lru_cache
from pathlib import Path

from app.strategy_tables.lib import candidate_infosets, encode_infoset
from app.strategy_tables.preflop_blueprint import blend_with_blueprint

DEFAULT_TABLE_CANDIDATES = [
    Path(__file__).resolve().parent / "strategy_tables" / "mllui_player_preflop_mccfr_6p_500000.json",
    Path(__file__).resolve().parent / "strategy_tables" / "tournament_blueprint_heads_up_cfr_10000000.json",
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
    return materialize_action(action_type, legal_actions, infoset, game_state, player_state)


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


def materialize_action(action_type, legal_actions, infoset, game_state=None, player_state=None):
    for action in legal_actions:
        if action["type"] != action_type:
            continue

        payload = {"type": action_type}
        if action_type in {"bet", "raise"}:
            payload["amount"] = choose_size(action, infoset, game_state, player_state)
        elif "amount" in action:
            payload["amount"] = action["amount"]
        return payload

    first = legal_actions[0]
    return {"type": first["type"], "amount": first.get("amount")}


def choose_size(action, infoset, game_state=None, player_state=None):
    # 戦略表には行動確率しかないため、ベット/レイズ額は局面と手札強度から
    # 明示的な候補サイズを重み付きで選びます。
    min_total = action["min_total"]
    max_total = action["max_total"]
    if max_total <= min_total:
        return max_total

    phase, _player_count, _position, bucket, _pressure, _stack_bucket, _texture = infoset.split("|")
    action_type = action["type"]
    target_total = choose_bet_target_total(action_type, phase, bucket, game_state, player_state)
    return max(min_total, min(max_total, int(target_total)))


def choose_bet_target_total(action_type, phase, bucket, game_state, player_state):
    if phase == "preflop" and (action_type == "bet" or is_preflop_open_raise(game_state)):
        big_blind = numeric_state_value(game_state, "big_blind", 10)
        multiplier = weighted_choice(
            [4.0, 3.0, 2.5],
            size_weights(bucket, ["large", "medium", "small"], preflop=True),
        )
        return big_blind * multiplier

    if action_type == "bet":
        pot = effective_pot(game_state)
        multiplier = weighted_choice(
            [1.0, 1.5, 2.0],
            size_weights(bucket, ["small", "medium", "large"]),
        )
        return player_round_bet(player_state) + pot * multiplier

    pot_after_call = effective_pot(game_state) + amount_to_call(game_state, player_state)
    multiplier = weighted_choice(
        [0.5, 1.0, 1.5],
        size_weights(bucket, ["small", "medium", "large"]),
    )
    return player_round_bet(player_state) + amount_to_call(game_state, player_state) + pot_after_call * multiplier


def size_weights(bucket, labels, preflop=False):
    strength = hand_strength_class(bucket, preflop)
    if strength == "strong":
        profile = {"small": 0.18, "medium": 0.32, "large": 0.50}
    elif strength == "medium":
        profile = {"small": 0.35, "medium": 0.45, "large": 0.20}
    else:
        profile = {"small": 0.60, "medium": 0.30, "large": 0.10}
    return [profile[label] for label in labels]


def hand_strength_class(bucket, preflop=False):
    if preflop:
        if bucket in {"premium", "strong"}:
            return "strong"
        if bucket in {"medium", "speculative"}:
            return "medium"
        return "weak"

    if bucket in {"monster", "made"}:
        return "strong"
    if bucket in {"strong_pair", "medium", "draw", "combo_draw"}:
        return "medium"
    return "weak"


def weighted_choice(values, weights):
    total = sum(weights)
    if total <= 0:
        return values[-1]
    threshold = random.random() * total
    cumulative = 0.0
    for value, weight in zip(values, weights):
        cumulative += weight
        if threshold <= cumulative:
            return value
    return values[-1]


def numeric_state_value(state, key, default):
    if not state:
        return default
    try:
        return float(state.get(key, default))
    except (TypeError, ValueError):
        return default


def effective_pot(game_state):
    return max(1.0, numeric_state_value(game_state, "pot", 1))


def player_round_bet(player_state):
    return numeric_state_value(player_state, "bet_round", 0)


def amount_to_call(game_state, player_state):
    current_bet = numeric_state_value(game_state, "current_bet", 0)
    return max(0.0, current_bet - player_round_bet(player_state))


def is_preflop_open_raise(game_state):
    current_bet = numeric_state_value(game_state, "current_bet", 0)
    big_blind = numeric_state_value(game_state, "big_blind", 10)
    return current_bet <= big_blind
