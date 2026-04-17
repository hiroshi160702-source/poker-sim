from __future__ import annotations

"""ヨコサワ式の考え方を元にしたプリフロップ土台戦略です。"""

from app.strategy_tables.lib import classify_position, classify_table_participants

COLOR_ORDER = ["ash", "pink", "purple", "white", "blue", "green", "yellow", "red", "navy"]
COLOR_TO_INDEX = {name: index for index, name in enumerate(COLOR_ORDER)}

HAND_COLOR_MAP = {
    "AA": "navy",
    "AKs": "navy",
    "AKo": "navy",
    "KK": "navy",
    "QQ": "navy",
    "AQs": "red",
    "AJs": "red",
    "ATs": "red",
    "KQs": "red",
    "AQo": "red",
    "JJ": "red",
    "TT": "red",
    "99": "red",
    "KJs": "yellow",
    "QJs": "yellow",
    "KQo": "yellow",
    "AJo": "yellow",
    "JTs": "yellow",
    "88": "yellow",
    "77": "yellow",
    "87o": "yellow",
    "A9s": "green",
    "A8s": "green",
    "A7s": "green",
    "A6s": "green",
    "A5s": "green",
    "A4s": "green",
    "A3s": "green",
    "A2s": "green",
    "KTs": "green",
    "K9s": "green",
    "QTs": "green",
    "KJo": "green",
    "ATo": "green",
    "T9s": "green",
    "66": "green",
    "55": "green",
    "Q9s": "blue",
    "QJo": "blue",
    "J9s": "blue",
    "JTo": "blue",
    "KTo": "blue",
    "T8s": "blue",
    "98s": "blue",
    "A9o": "blue",
    "44": "blue",
    "33": "blue",
    "22": "blue",
    "K8s": "white",
    "K7s": "white",
    "K6s": "white",
    "K5s": "white",
    "K4s": "white",
    "K3s": "white",
    "K2s": "white",
    "Q8s": "white",
    "Q7s": "white",
    "Q6s": "white",
    "J8s": "white",
    "J7s": "white",
    "QTo": "white",
    "K9o": "white",
    "Q9o": "white",
    "J9o": "white",
    "T9o": "white",
    "A8o": "white",
    "97s": "white",
    "87s": "white",
    "76s": "white",
    "65s": "white",
    "Q5s": "purple",
    "Q4s": "purple",
    "Q3s": "purple",
    "Q2s": "purple",
    "J6s": "purple",
    "T7s": "purple",
    "96s": "purple",
    "98o": "purple",
    "86s": "purple",
    "75s": "purple",
    "A6o": "purple",
    "64s": "purple",
    "54s": "purple",
    "J5s": "pink",
    "J4s": "pink",
    "J3s": "pink",
    "J2s": "pink",
    "T6s": "pink",
    "T5s": "pink",
    "T4s": "pink",
    "T3s": "pink",
    "95s": "pink",
    "K8o": "pink",
    "Q8o": "pink",
    "J8o": "pink",
    "T8o": "pink",
    "K7o": "pink",
    "Q7o": "pink",
    "97o": "pink",
    "K6o": "pink",
    "63s": "pink",
    "A5o": "pink",
    "K5o": "pink",
    "53s": "pink",
    "A4o": "pink",
    "43s": "pink",
    "A3o": "pink",
    "A2o": "pink",
}


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    filtered = {action: max(0.0, value) for action, value in weights.items()}
    total = sum(filtered.values())
    if total <= 0:
        uniform = 1.0 / len(filtered)
        return {action: uniform for action in filtered}
    return {action: value / total for action, value in filtered.items()}


def infer_hand_color(bucket: str, hand: list[str] | None = None) -> str:
    if not hand:
        return {
            "premium": "red",
            "strong": "yellow",
            "medium": "green",
            "speculative": "white",
            "weak": "ash",
        }.get(bucket, "ash")

    return HAND_COLOR_MAP.get(canonical_hand_key(hand), "ash")


def canonical_hand_key(hand: list[str]) -> str:
    ranks = sorted([card[0] for card in hand], key=rank_value, reverse=True)
    if ranks[0] == ranks[1]:
        return f"{ranks[0]}{ranks[1]}"
    suited = hand[0][1] == hand[1][1]
    suffix = "s" if suited else "o"
    return f"{ranks[0]}{ranks[1]}{suffix}"


def rank_value(rank: str) -> int:
    order = "23456789TJQKA"
    return order.index(rank) + 2


def behind_threshold(position: str, player_count: str) -> str:
    players = int(player_count[:-1]) if player_count.endswith("p") and player_count[:-1].isdigit() else 6
    if position == "button":
        return "purple"
    if position == "late":
        return "white"
    if position == "middle":
        return "blue" if players <= 6 else "green"
    if position == "early":
        return "green"
    if position in {"blind", "big_blind"}:
        return "pink"
    return "white"


def color_at_least(color: str, minimum: str) -> bool:
    return COLOR_TO_INDEX.get(color, 0) >= COLOR_TO_INDEX.get(minimum, 0)


def color_plus(minimum: str, steps: int) -> str:
    return COLOR_ORDER[min(len(COLOR_ORDER) - 1, COLOR_TO_INDEX[minimum] + steps)]


def infer_opener_min_color(
    infoset: str,
    game_state: dict | None,
    player_state: dict | None,
) -> str:
    phase, player_count, position, _bucket, _pressure, _stack_bucket, _texture = infoset.split("|")
    if phase != "preflop":
        return "white"

    if game_state and player_state:
        aggressor = find_preflop_aggressor(game_state, player_state)
        if aggressor:
            aggressor_position = classify_position(game_state, aggressor)
            aggressor_count = classify_table_participants(game_state)
            return behind_threshold(aggressor_position, aggressor_count)

    if position == "button":
        return "white"
    if position == "big_blind":
        return "white"
    return behind_threshold(position, player_count)


def find_preflop_aggressor(game_state: dict, player_state: dict) -> dict | None:
    current_bet = game_state.get("current_bet", 0)
    if current_bet <= 50:
        return None

    candidates = [
        player
        for player in game_state.get("players", [])
        if player["seat"] != player_state["seat"]
        and player.get("bet_round", 0) == current_bet
        and player.get("in_hand")
        and not player.get("folded")
    ]
    if not candidates:
        return None

    aggressive = [
        player for player in candidates
        if any(
            keyword in (player.get("last_action") or "").lower()
            for keyword in ("raise", "bet", "all-in")
        )
    ]
    if aggressive:
        return aggressive[0]
    return candidates[0]


def build_preflop_blueprint(
    infoset: str,
    legal_actions: list[dict],
    game_state: dict | None = None,
    player_state: dict | None = None,
) -> dict[str, float] | None:
    parts = infoset.split("|")
    if len(parts) != 7 or parts[0] != "preflop":
        return None

    _phase, player_count, position, bucket, pressure, stack_bucket, _texture = parts
    legal_types = [action["type"] for action in legal_actions]
    weights = {action_type: 0.02 for action_type in legal_types}

    hand = player_state.get("actual_hand") if player_state else None
    color = infer_hand_color(bucket, hand)
    open_min = behind_threshold(position, player_count)
    call_min = color_plus(infer_opener_min_color(infoset, game_state, player_state), 1)
    threebet_min = color_plus(infer_opener_min_color(infoset, game_state, player_state), 2)
    shallow = stack_bucket == "shallow"
    unopened = pressure == "none"
    facing_raise = pressure in {"tiny", "small", "medium", "large", "jam"}
    big_blind = position == "big_blind"

    def bump(action_type: str, amount: float) -> None:
        if action_type in weights:
            weights[action_type] += amount

    if unopened:
        if color_at_least(color, open_min):
            if color_at_least(color, "red"):
                bump("raise", 8.0)
                bump("bet", 8.0)
                bump("call", 0.6)
                bump("fold", 0.02)
                bump("all-in", 0.25 if shallow else 0.08)
            elif color_at_least(color, "yellow"):
                bump("raise", 6.4)
                bump("bet", 6.4)
                bump("call", 0.8)
                bump("fold", 0.08)
                bump("all-in", 0.16 if shallow else 0.03)
            elif color_at_least(color, "green"):
                bump("raise", 4.5)
                bump("bet", 4.5)
                bump("call", 1.1)
                bump("fold", 0.3)
                bump("all-in", 0.08 if shallow else 0.01)
            elif color_at_least(color, "white"):
                bump("raise", 2.8)
                bump("bet", 2.8)
                bump("call", 1.0)
                bump("fold", 1.2)
            else:
                bump("raise", 1.6)
                bump("bet", 1.6)
                bump("call", 0.8)
                bump("fold", 1.8)
        else:
            bump("fold", 8.5)
            bump("check", 7.0)
            bump("call", 0.4)
            bump("raise", 0.08)
            bump("bet", 0.08)
            bump("all-in", 0.01)
        return normalize_weights(weights)

    if big_blind:
        opener_min = infer_opener_min_color(infoset, game_state, player_state)
        if opener_min in {"purple", "pink"}:
            defend_min = "pink"
        elif opener_min in {"white", "blue"}:
            defend_min = "purple"
        else:
            defend_min = "white"

        if color_at_least(color, color_plus(opener_min, 2)):
            bump("raise", 6.8)
            bump("call", 2.6)
            bump("fold", 0.1)
            bump("all-in", 0.18 if shallow else 0.04)
        elif color_at_least(color, defend_min):
            bump("call", 7.2)
            bump("raise", 1.2 if color_at_least(color, call_min) else 0.25)
            bump("fold", 0.8)
            bump("all-in", 0.05 if shallow and color_at_least(color, threebet_min) else 0.01)
        else:
            bump("fold", 8.8)
            bump("call", 0.35)
            bump("raise", 0.04)
        return normalize_weights(weights)

    if facing_raise:
        if color_at_least(color, threebet_min):
            bump("raise", 7.0)
            bump("call", 2.3)
            bump("fold", 0.08)
            bump("all-in", 0.3 if shallow and color_at_least(color, "red") else 0.04)
        elif color_at_least(color, call_min):
            bump("call", 6.5)
            bump("raise", 0.9 if color_at_least(color, "green") else 0.25)
            bump("fold", 1.0)
            bump("all-in", 0.04 if shallow and color_at_least(color, "red") else 0.01)
        else:
            bump("fold", 9.0)
            bump("call", 0.25)
            bump("raise", 0.03)
            bump("all-in", 0.005)
        return normalize_weights(weights)

    return normalize_weights(weights)


def blend_with_blueprint(
    strategy: dict[str, float],
    infoset: str,
    legal_actions: list[dict],
    table_weight: float,
    game_state: dict | None = None,
    player_state: dict | None = None,
) -> dict[str, float]:
    blueprint = build_preflop_blueprint(infoset, legal_actions, game_state, player_state)
    if not blueprint:
        return normalize_weights(dict(strategy))

    table_weight = min(1.0, max(0.0, table_weight))
    blueprint_weight = 1.0 - table_weight
    actions = set(strategy) | set(blueprint)
    mixed = {
        action: strategy.get(action, 0.0) * table_weight + blueprint.get(action, 0.0) * blueprint_weight
        for action in actions
    }
    return normalize_weights(mixed)
