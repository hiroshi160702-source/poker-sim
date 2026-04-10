import random

from app.strategy_tables.lib import classify_postflop, classify_preflop


def decide_action(game_state, player_state, legal_actions):
    phase = game_state["phase"]
    if phase == "preflop":
        bucket = classify_preflop(player_state["actual_hand"])
        return choose_preflop(bucket, legal_actions)
    bucket = classify_postflop(player_state["actual_hand"], game_state["community_cards"])
    return choose_postflop(bucket, legal_actions)


def choose_preflop(bucket, legal_actions):
    weights = {
        "premium": {"raise": 8, "call": 3, "all-in": 1, "check": 1},
        "strong": {"raise": 6, "call": 5, "check": 2},
        "medium": {"call": 7, "raise": 3, "check": 4, "fold": 1},
        "speculative": {"call": 7, "raise": 2, "check": 5, "fold": 1},
        "weak": {"call": 4, "check": 6, "fold": 2},
    }
    action_type = weighted_choice(legal_actions, weights.get(bucket, {}), prefer_passive=True)
    return materialize_action(action_type, legal_actions, phase="preflop", bucket=bucket)


def choose_postflop(bucket, legal_actions):
    weights = {
        "monster": {"bet": 8, "raise": 8, "call": 3, "all-in": 2},
        "made": {"bet": 7, "raise": 6, "call": 5, "check": 3},
        "strong_pair": {"bet": 4, "raise": 2, "call": 7, "check": 5},
        "combo_draw": {"bet": 5, "raise": 4, "call": 8, "check": 4, "fold": 1},
        "draw": {"bet": 4, "raise": 3, "call": 8, "check": 5, "fold": 1},
        "marginal": {"call": 6, "check": 7, "bet": 2, "fold": 2},
        "air": {"check": 8, "call": 3, "bet": 2, "fold": 3},
    }
    action_type = weighted_choice(legal_actions, weights.get(bucket, {}), prefer_passive=False)
    return materialize_action(action_type, legal_actions, phase="postflop", bucket=bucket)


def weighted_choice(legal_actions, preferred_weights, prefer_passive):
    weighted = []
    for action in legal_actions:
        action_type = action["type"]
        weight = preferred_weights.get(action_type, 0)
        if weight <= 0:
            if action_type in {"check", "call"}:
                weight = 4 if prefer_passive else 3
            elif action_type in {"bet", "raise"}:
                weight = 2
            elif action_type == "fold":
                weight = 1
            else:
                weight = 1
        weighted.append((action_type, weight))
    total = sum(weight for _, weight in weighted)
    threshold = random.uniform(0, total)
    cumulative = 0.0
    for action_type, weight in weighted:
        cumulative += weight
        if threshold <= cumulative:
            return action_type
    return weighted[-1][0]


def materialize_action(action_type, legal_actions, phase, bucket):
    for action in legal_actions:
        if action["type"] != action_type:
            continue
        payload = {"type": action_type}
        if action_type in {"bet", "raise"}:
            payload["amount"] = choose_size(action, phase, bucket)
        elif "amount" in action:
            payload["amount"] = action["amount"]
        return payload
    first = legal_actions[0]
    return {"type": first["type"], "amount": first.get("amount")}


def choose_size(action, phase, bucket):
    min_total = action["min_total"]
    max_total = action["max_total"]
    if max_total <= min_total:
        return max_total

    span = max_total - min_total
    if phase == "preflop":
        if bucket in {"premium", "strong"}:
            factor = 0.35
        elif bucket == "speculative":
            factor = 0.18
        else:
            factor = 0.12
    else:
        if bucket == "made":
            factor = 0.45
        elif bucket == "draw":
            factor = 0.28
        elif bucket == "marginal":
            factor = 0.18
        else:
            factor = 0.12
    return max(min_total, min(max_total, min_total + int(span * factor)))
