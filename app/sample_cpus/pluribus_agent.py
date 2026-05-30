from __future__ import annotations

"""Pluribus-inspired sample CPU.

This is not the original Pluribus implementation. It follows the same practical
shape that fits this simulator: a compact blueprint policy, action abstraction
into fold/check/call plus 1/3-pot, 1/2-pot, pot, and all-in sizes, and a small
real-time Monte Carlo search to choose between those abstract actions.
"""

import itertools
import json
import math
import os
import random
import re
import time
from json import JSONDecodeError
from pathlib import Path

from app.strategy_tables.lib import candidate_infosets, encode_infoset
from app.strategy_tables.preflop_blueprint import COLOR_TO_INDEX, infer_hand_color

RANKS = "23456789TJQKA"
SUITS = "SHDC"
VALUES = {rank: index for index, rank in enumerate(RANKS, start=2)}
MAX_SUBGAME_SECONDS = 15.0
DEFAULT_SUBGAME_SECONDS = 2.5
SUBGAME_HAND_CAP = 132
DEFAULT_BLUEPRINT_CANDIDATES = [
    Path(__file__).resolve().parent / "strategy_tables" / "multiway_mccfr.json",
    Path(__file__).resolve().parent / "strategy_tables" / "multiway_6max_mccfr.json",
    Path(__file__).resolve().parent / "strategy_tables" / "multiplayer_strategy_6p_5000000hands.json",
    Path(__file__).resolve().parent / "strategy_tables" / "mllui_player_preflop_mccfr_6p_500000.json",
]
_BLUEPRINT_CACHE = {
    "path": None,
    "mtime": None,
    "table": {},
}
_LATEST_BLUEPRINT_CACHE = {
    "checked_at": -999999.0,
    "path": None,
    "signature": None,
}


def decide_action(game_state, player_state, legal_actions):
    decision, _trace = decide_action_with_trace(game_state, player_state, legal_actions)
    return decision


def decide_action_with_trace(game_state, player_state, legal_actions):
    context = build_context(game_state, player_state)
    candidates = build_action_candidates(legal_actions, context)
    if not candidates:
        decision = {"type": "fold"}
        return decision, {
            "decision": decision,
            "reason": "no legal candidates after abstraction",
            "legal_actions": legal_actions,
        }

    infoset = encode_infoset(game_state, player_state)
    ranges = update_ranges(game_state, player_state, infoset)
    blueprint = blueprint_strategy(game_state, player_state, legal_actions, candidates, context, infoset)
    subgame_strategy = solve_subgame(
        context=context,
        candidates=candidates,
        blueprint=blueprint,
        ranges=ranges,
        deadline=time.perf_counter() + subgame_seconds(),
    )
    subgame_strategy = apply_runtime_safety(context, candidates, subgame_strategy)

    chosen = weighted_choice(candidates, subgame_strategy)
    decision = materialize(chosen)
    trace = build_decision_trace(
        game_state=game_state,
        player_state=player_state,
        legal_actions=legal_actions,
        context=context,
        infoset=infoset,
        ranges=ranges,
        candidates=candidates,
        blueprint=blueprint,
        final_strategy=subgame_strategy,
        chosen=chosen,
        decision=decision,
    )
    return decision, trace


def build_decision_trace(
    *,
    game_state,
    player_state,
    legal_actions,
    context,
    infoset,
    ranges,
    candidates,
    blueprint,
    final_strategy,
    chosen,
    decision,
):
    return {
        "hand_id": game_state.get("hand_id"),
        "phase": game_state.get("phase"),
        "seat": player_state.get("seat"),
        "hand": list(player_state.get("actual_hand", [])),
        "board": list(game_state.get("community_cards", [])),
        "infoset": infoset,
        "context": trace_context(context),
        "blueprint_path": _BLUEPRINT_CACHE.get("path"),
        "legal_actions": legal_actions,
        "candidates": [
            {
                **candidate,
                "key": candidate_key(candidate),
                "table_action": table_action_name(candidate),
                "blueprint_probability": round(blueprint.get(candidate_key(candidate), 0.0), 8),
                "final_probability": round(final_strategy.get(candidate_key(candidate), 0.0), 8),
            }
            for candidate in candidates
        ],
        "opponent_ranges": ranges,
        "chosen_candidate": {
            **chosen,
            "key": candidate_key(chosen),
            "table_action": table_action_name(chosen),
        },
        "decision": decision,
        "explanation": explanation_lines(context, candidates, blueprint, final_strategy, chosen),
    }


def trace_context(context):
    keys = [
        "phase",
        "hand",
        "board",
        "pot",
        "to_call",
        "stack",
        "effective_stack",
        "stack_bb",
        "spr",
        "pot_odds",
        "equity",
        "made_rank",
        "draw_score",
        "texture",
        "position",
        "pressure",
        "opponents",
        "active_players",
    ]
    result = {}
    for key in keys:
        value = context.get(key)
        result[key] = round(value, 6) if isinstance(value, float) else value
    result["preflop_color"] = infer_hand_color("", context["hand"]) if context["phase"] == "preflop" else None
    return result


def explanation_lines(context, candidates, blueprint, final_strategy, chosen):
    chosen_key = candidate_key(chosen)
    ranked = sorted(
        (
            (
                candidate_key(candidate),
                table_action_name(candidate),
                final_strategy.get(candidate_key(candidate), 0.0),
                blueprint.get(candidate_key(candidate), 0.0),
            )
            for candidate in candidates
        ),
        key=lambda item: item[2],
        reverse=True,
    )
    lines = [
        f"infoset context: phase={context['phase']}, position={context['position']}, pressure={context['pressure']}, stack_bb={context['stack_bb']:.1f}",
        f"hand strength inputs: equity={context['equity']:.3f}, pot_odds={context['pot_odds']:.3f}, draw_score={context['draw_score']:.3f}, texture={context['texture']}",
        "final strategy ranking: "
        + ", ".join(
            f"{action_name} final={probability:.3f} blueprint={blueprint_probability:.3f}"
            for _key, action_name, probability, blueprint_probability in ranked[:6]
        ),
        f"sampled action: {table_action_name(chosen)} ({chosen_key})",
    ]
    if context["phase"] == "preflop":
        lines.append(
            f"preflop color={infer_hand_color('', context['hand'])}; runtime safety suppresses deep-stack weak all-in branches"
        )
    return lines


def load_blueprint(path_hint=None):
    path = resolve_blueprint_path(path_hint)
    if not path:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _BLUEPRINT_CACHE["table"] if _BLUEPRINT_CACHE["path"] == str(path) else {}

    if _BLUEPRINT_CACHE["path"] == str(path) and _BLUEPRINT_CACHE["mtime"] == mtime:
        return _BLUEPRINT_CACHE["table"]

    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        # 学習プロセスがJSONを書き換えている瞬間は一時的に読めないことがあります。
        # その場合は直前に読めたblueprintを使い、次の意思決定で再試行します。
        return _BLUEPRINT_CACHE["table"] if _BLUEPRINT_CACHE["path"] == str(path) else {}

    _BLUEPRINT_CACHE["path"] = str(path)
    _BLUEPRINT_CACHE["mtime"] = mtime
    _BLUEPRINT_CACHE["table"] = table
    return table


def resolve_blueprint_path(path_hint=None):
    pinned = os.environ.get("PLURIBUS_BLUEPRINT_PINNED", "").lower() in {"1", "true", "yes"}
    auto_latest = os.environ.get("PLURIBUS_BLUEPRINT_AUTO", "1").lower() not in {"0", "false", "no"}
    env_path = os.environ.get("PLURIBUS_BLUEPRINT_PATH")

    if pinned:
        candidates = []
        if path_hint:
            candidates.append(Path(path_hint).expanduser())
        if env_path:
            candidates.append(Path(env_path).expanduser())
        candidates.extend(DEFAULT_BLUEPRINT_CANDIDATES)
        return first_existing_path(candidates)

    if auto_latest:
        latest = latest_blueprint_path()
        if latest:
            return latest

    candidates = []
    if path_hint:
        candidates.append(Path(path_hint).expanduser())
    if env_path and env_path.lower() != "auto":
        candidates.append(Path(env_path).expanduser())
    candidates.extend(DEFAULT_BLUEPRINT_CANDIDATES)
    sibling_jsons = sorted(Path(__file__).resolve().parent.glob("*.json"))
    candidates.extend(sibling_jsons)
    return first_existing_path(candidates)


def first_existing_path(candidates):
    for candidate in candidates:
        path = candidate.resolve()
        if path.exists():
            return path
    return None


def latest_blueprint_path():
    now = time.monotonic()
    if _LATEST_BLUEPRINT_CACHE["signature"] is not None and now - _LATEST_BLUEPRINT_CACHE["checked_at"] < 2.0:
        cached_path = _LATEST_BLUEPRINT_CACHE["path"]
        return Path(cached_path) if cached_path else None

    table_dir = Path(__file__).resolve().parent / "strategy_tables"
    candidates = sorted(table_dir.glob("pluribus_blueprint_6p_*.json"))
    candidates = [
        path
        for path in candidates
        if not path.name.endswith("_state.json")
        and not path.name.endswith("_visits.json")
        and ".legacy_" not in path.name
        and ".pre_color_legacy_" not in path.name
    ]
    signature = tuple(
        (str(path), safe_mtime(path), safe_mtime(blueprint_state_path(path))) for path in candidates
    )
    if signature == _LATEST_BLUEPRINT_CACHE["signature"]:
        _LATEST_BLUEPRINT_CACHE["checked_at"] = now
        cached_path = _LATEST_BLUEPRINT_CACHE["path"]
        return Path(cached_path) if cached_path else None

    best = None
    for path in candidates:
        iterations = blueprint_iterations(path)
        score = (iterations, safe_mtime(path), str(path))
        if best is None or score > best[0]:
            best = (score, path)

    _LATEST_BLUEPRINT_CACHE["checked_at"] = now
    _LATEST_BLUEPRINT_CACHE["signature"] = signature
    _LATEST_BLUEPRINT_CACHE["path"] = str(best[1]) if best else None
    return best[1] if best else None


def blueprint_state_path(path):
    return path.with_name(f"{path.stem}_state.json")


def blueprint_iterations(path):
    state_path = blueprint_state_path(path)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            completed = state.get("completed_iterations") or state.get("completed_iterations_total")
            if isinstance(completed, (int, float)):
                return int(completed)
        except (OSError, JSONDecodeError, TypeError, ValueError):
            pass

    match = re.search(r"_6p_(\d+)$", path.stem)
    if match:
        return int(match.group(1))
    if "continuous" in path.stem:
        return 0
    return 0


def safe_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def subgame_seconds():
    raw = os.environ.get("PLURIBUS_SUBGAME_SECONDS", str(DEFAULT_SUBGAME_SECONDS))
    try:
        requested = float(raw)
    except ValueError:
        requested = DEFAULT_SUBGAME_SECONDS
    return max(0.05, min(MAX_SUBGAME_SECONDS, requested))


def build_context(game_state, player_state):
    hand = list(player_state["actual_hand"])
    board = list(game_state.get("community_cards", []))
    pot = max(1, int(game_state.get("pot", 0)))
    current_bet = int(game_state.get("current_bet", 0))
    bet_round = int(player_state.get("bet_round", 0))
    to_call = max(0, current_bet - bet_round)
    stack = max(0, int(player_state.get("stack", 0)))
    active = [
        player
        for player in game_state.get("players", [])
        if player.get("in_hand") and not player.get("folded")
    ]
    opponents = max(1, len(active) - 1)
    effective_stack = min(
        [stack]
        + [
            int(player.get("stack", 0))
            for player in active
            if player.get("seat") != player_state.get("seat")
        ]
    )
    equity = estimate_equity(hand, board, opponents, samples=samples_for_phase(game_state.get("phase")))
    made_rank = best_rank(hand + board) if len(hand) + len(board) >= 5 else 0
    draw_score = draw_potential(hand, board)
    return {
        "phase": game_state.get("phase", "preflop"),
        "hand": hand,
        "board": board,
        "big_blind": max(1, int(game_state.get("big_blind", 50))),
        "pot": pot,
        "to_call": to_call,
        "stack": stack,
        "bet_round": bet_round,
        "opponents": opponents,
        "active_players": len(active),
        "effective_stack": effective_stack,
        "stack_bb": effective_stack / max(1, int(game_state.get("big_blind", 50))),
        "spr": effective_stack / pot,
        "pot_odds": to_call / (pot + to_call) if to_call > 0 else 0.0,
        "equity": equity,
        "made_rank": made_rank,
        "draw_score": draw_score,
        "texture": board_texture(board),
        "position": classify_position(game_state, player_state),
        "pressure": classify_pressure(to_call, pot, stack),
    }


def update_ranges(game_state, player_state, infoset):
    # Public actions are converted into coarse range weights. This gives the
    # subgame search a living model of opponents instead of treating every
    # unseen hand as equally likely after large bets or passive lines.
    ranges = {}
    board = list(game_state.get("community_cards", []))
    for player in game_state.get("players", []):
        seat = player.get("seat")
        if seat == player_state.get("seat") or not player.get("in_hand") or player.get("folded"):
            continue
        pressure = classify_pressure(
            max(0, game_state.get("current_bet", 0) - player.get("bet_round", 0)),
            max(1, game_state.get("pot", 1)),
            max(1, player.get("stack", 1)),
        )
        last_action = (player.get("last_action") or "").lower()
        ranges[seat] = {
            "tightness": range_tightness(last_action, pressure),
            "aggression": range_aggression(last_action),
            "board": board,
            "infoset": infoset,
        }
    return ranges


def range_tightness(last_action, pressure):
    tightness = 1.0
    if any(word in last_action for word in ("raise", "bet", "all-in")):
        tightness += 0.55
    if "call" in last_action:
        tightness += 0.20
    if "check" in last_action:
        tightness -= 0.18
    if pressure in {"large", "jam"}:
        tightness += 0.35
    return max(0.45, min(2.1, tightness))


def range_aggression(last_action):
    if "all-in" in last_action:
        return 1.8
    if "raise" in last_action:
        return 1.55
    if "bet" in last_action:
        return 1.35
    if "call" in last_action:
        return 0.9
    return 0.7


def samples_for_phase(phase):
    if phase == "preflop":
        return 80
    if phase == "flop":
        return 70
    return 55


def build_action_candidates(legal_actions, context):
    candidates = []
    strategy_all_in_allowed = all_in_as_strategy_action_allowed(legal_actions)
    for action in legal_actions:
        action_type = action["type"]
        if action_type == "all-in" and not strategy_all_in_allowed:
            continue
        if action_type in {"fold", "check", "call", "all-in"}:
            candidates.append(
                {
                    "type": action_type,
                    "size": action_type,
                    "amount": action.get("amount"),
                    "total": context["bet_round"] + int(action.get("amount", 0) or 0),
                }
            )
            continue

        if action_type in {"bet", "raise"}:
            sizes = action.get("abstract_sizes") or fallback_sizes(action, context)
            for size in sizes:
                if size["name"] == "all-in":
                    continue
                candidates.append(
                    {
                        "type": action_type,
                        "size": size["name"],
                        "amount": int(size["total"]),
                        "total": int(size["total"]),
                    }
                )

    return dedupe_candidates(candidates)


def all_in_as_strategy_action_allowed(legal_actions):
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


def fallback_sizes(action, context):
    min_total = int(action["min_total"])
    max_total = int(action["max_total"])
    to_call = context["to_call"]
    if action["type"] == "bet":
        base = context["bet_round"]
        sizing_pot = max(context["pot"], 1)
    else:
        base = context["bet_round"] + to_call
        sizing_pot = max(context["pot"] + to_call, 1)
    raw = [
        ("small", base + sizing_pot / 3),
        ("medium", base + sizing_pot / 2),
        ("large", base + sizing_pot),
        ("all-in", max_total),
    ]
    return [
        {"name": name, "total": max(min_total, min(max_total, round_to_unit(total)))}
        for name, total in raw
    ]


def dedupe_candidates(candidates):
    result = []
    seen = set()
    for candidate in candidates:
        key = (candidate["type"], candidate.get("total"), candidate.get("size"))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def blueprint_strategy(game_state, player_state, legal_actions, candidates, context, infoset):
    table = load_blueprint()
    blueprint_candidate_strategy = lookup_blueprint_candidate_strategy(table, infoset, candidates)
    if not blueprint_candidate_strategy:
        return blueprint_weights(context, candidates)

    fallback = blueprint_weights(context, candidates)
    mixed = {
        key: 0.78 * blueprint_candidate_strategy.get(key, 0.0) + 0.22 * fallback.get(key, 0.0)
        for key in {candidate_key(candidate) for candidate in candidates}
    }
    return normalize(mixed)


def lookup_blueprint_candidate_strategy(table, infoset, candidates):
    if not table:
        return None
    for key in candidate_infosets(infoset):
        strategy = table.get(key)
        if not strategy:
            continue
        filtered = candidate_strategy_from_table_row(strategy, candidates)
        if filtered:
            return normalize(filtered)
    return interpolated_blueprint_candidate_strategy(table, infoset, candidates)


def interpolated_blueprint_candidate_strategy(table, infoset, candidates):
    target_parts = infoset.split("|")
    if len(target_parts) != 7:
        return None

    for wildcard_indexes in interpolation_wildcard_indexes():
        aggregate = {candidate_key(candidate): 0.0 for candidate in candidates}
        matched = 0
        for row_key, row_strategy in table.items():
            row_parts = row_key.split("|")
            if len(row_parts) != 7:
                continue
            if not infoset_parts_match(target_parts, row_parts, wildcard_indexes):
                continue
            filtered = candidate_strategy_from_table_row(row_strategy, candidates)
            if not filtered:
                continue
            matched += 1
            normalized = normalize(filtered)
            for key, probability in normalized.items():
                aggregate[key] += probability
        if matched:
            return normalize(aggregate)
    return None


def interpolation_wildcard_indexes():
    return (
        (5,),
        (1,),
        (6,),
        (2,),
        (4,),
        (3,),
        (1, 5),
        (5, 6),
        (2, 5),
        (3, 5),
        (1, 6),
        (1, 2),
        (2, 4),
        (3, 4),
        (1, 2, 5),
        (3, 5, 6),
        (1, 2, 3),
        (1, 2, 3, 4),
        (2, 3, 4, 5, 6),
    )


def infoset_parts_match(target_parts, row_parts, wildcard_indexes):
    wildcard_indexes = set(wildcard_indexes)
    for index, target in enumerate(target_parts):
        if index in wildcard_indexes:
            continue
        row_value = row_parts[index]
        if row_value != target and row_value != "any":
            return False
    return True


def candidate_strategy_from_table_row(strategy, candidates):
    weights = {candidate_key(candidate): 0.0 for candidate in candidates}
    base_buckets = {}
    for candidate in candidates:
        action_name = table_action_name(candidate)
        base_buckets.setdefault(candidate["type"], []).append(candidate)
        if action_name in strategy:
            weights[candidate_key(candidate)] += float(strategy[action_name])

    for base_action, probability in strategy.items():
        if "_" in base_action or base_action not in {"bet", "raise"}:
            continue
        matching = base_buckets.get(base_action, [])
        if not matching:
            continue
        share = float(probability) / len(matching)
        for candidate in matching:
            weights[candidate_key(candidate)] += share

    return {key: value for key, value in weights.items() if value > 0}


def table_action_name(candidate):
    if candidate["type"] in {"bet", "raise"} and candidate["size"] in {"small", "medium", "large"}:
        return f"{candidate['type']}_{candidate['size']}"
    return candidate["type"]


def blueprint_weights(context, candidates):
    weights = {}
    equity = context["equity"]
    pressure = context["pressure"]
    phase = context["phase"]
    draw = context["draw_score"]
    multiway_penalty = 0.90 ** max(0, context["opponents"] - 1)

    for candidate in candidates:
        action_type = candidate["type"]
        size = candidate["size"]
        weight = 0.05
        if action_type == "fold":
            weight = 0.15 + max(0.0, context["pot_odds"] - equity) * 6.0
        elif action_type == "check":
            weight = 0.75 - max(0.0, equity - 0.55)
        elif action_type == "call":
            weight = 0.35 + max(0.0, equity - context["pot_odds"]) * 3.2 + draw * 0.8
        elif action_type in {"bet", "raise"}:
            value_band = max(0.0, equity - 0.48) * 3.0
            bluff_band = max(0.0, 0.34 - equity) * (0.8 + draw)
            weight = (value_band + bluff_band) * multiway_penalty
            if size == "small":
                weight *= 1.20 if phase != "river" else 0.90
            elif size == "medium":
                weight *= 1.05
            elif size == "large":
                weight *= 0.85 + max(0.0, equity - 0.70) * 1.3
            elif size == "all-in":
                weight *= 0.25 + max(0.0, equity - 0.82) * 4.0
        elif action_type == "all-in":
            weight = max(0.002, (equity - 0.86) * 2.5)

        if pressure in {"large", "jam"} and action_type in {"bet", "raise"}:
            weight *= 0.7
        weights[candidate_key(candidate)] = max(0.01, weight)

    return normalize(weights)


def apply_runtime_safety(context, candidates, strategy):
    weights = dict(strategy)
    if context["phase"] == "preflop":
        multiplier = preflop_all_in_multiplier(context)
        for candidate in candidates:
            key = candidate_key(candidate)
            if candidate["type"] == "all-in":
                weights[key] = weights.get(key, 0.0) * multiplier
            elif candidate["type"] in {"call", "raise"} and multiplier < 0.05:
                # Deep stacked non-premium hands should prefer ordinary calls/raises
                # over the all-in branch when the search is noisy.
                weights[key] = weights.get(key, 0.0) * 1.08
    return normalize(weights)


def preflop_all_in_multiplier(context):
    color = infer_hand_color("", context["hand"])
    color_index = COLOR_TO_INDEX.get(color, 0)
    navy = COLOR_TO_INDEX["navy"]
    red = COLOR_TO_INDEX["red"]
    yellow = COLOR_TO_INDEX["yellow"]
    green = COLOR_TO_INDEX["green"]
    stack_bb = context["stack_bb"]
    pressure = context["pressure"]

    if stack_bb <= 12:
        if color_index >= red:
            return 1.0
        if color_index >= yellow:
            return 0.55
        if color_index >= green:
            return 0.22
        return 0.04

    if stack_bb <= 35:
        if color_index >= navy:
            return 0.32 if pressure in {"large", "jam"} else 0.18
        if color_index >= red:
            return 0.09 if pressure in {"large", "jam"} else 0.035
        if color_index >= yellow:
            return 0.018
        return 0.003

    if color_index >= navy:
        return 0.055 if pressure in {"large", "jam"} else 0.018
    if color_index >= red:
        return 0.008
    return 0.001


def solve_subgame(context, candidates, blueprint, ranges, deadline):
    private_hands = subgame_private_hands(context["hand"], context["board"], deadline)
    regrets = {
        hand_key(hand): {candidate_key(candidate): 0.0 for candidate in candidates}
        for hand in private_hands
    }
    strategy_sums = {
        hand_key(hand): {candidate_key(candidate): 0.0 for candidate in candidates}
        for hand in private_hands
    }

    visited_actual = False
    while time.perf_counter() < deadline:
        for private_hand in private_hands:
            if time.perf_counter() >= deadline:
                break
            key = hand_key(private_hand)
            if key == hand_key(context["hand"]):
                visited_actual = True
            strategy = regret_matching(regrets[key], blueprint)
            hand_context = context_for_private_hand(context, private_hand, ranges)
            utilities = {
                candidate_key(candidate): estimate_action_ev(hand_context, candidate)
                for candidate in candidates
            }
            node_utility = sum(strategy[action_key] * utilities[action_key] for action_key in strategy)
            for action_key, utility in utilities.items():
                regrets[key][action_key] += utility - node_utility
                strategy_sums[key][action_key] += strategy[action_key]

    actual_key = hand_key(context["hand"])
    if not visited_actual:
        strategy = regret_matching(regrets[actual_key], blueprint)
        hand_context = context_for_private_hand(context, context["hand"], ranges)
        utilities = {
            candidate_key(candidate): estimate_action_ev(hand_context, candidate)
            for candidate in candidates
        }
        node_utility = sum(strategy[action_key] * utilities[action_key] for action_key in strategy)
        for action_key, utility in utilities.items():
            regrets[actual_key][action_key] += utility - node_utility
            strategy_sums[actual_key][action_key] += strategy[action_key]

    if actual_key not in strategy_sums:
        return blueprint
    average = normalize(strategy_sums[actual_key])
    if not average:
        return blueprint
    return {
        key: 0.25 * blueprint.get(key, 0.0) + 0.75 * average.get(key, 0.0)
        for key in {candidate_key(candidate) for candidate in candidates}
    }


def regret_matching(regrets, blueprint):
    positive = {key: max(0.0, value) for key, value in regrets.items()}
    total = sum(positive.values())
    if total > 0:
        return {key: value / total for key, value in positive.items()}
    return dict(blueprint)


def subgame_private_hands(actual_hand, board, deadline):
    blocked = set(board)
    deck = [card for card in make_deck() if card not in blocked]
    all_hands = [list(combo) for combo in itertools.combinations(deck, 2)]
    actual_key = hand_key(actual_hand)
    all_hands.sort(key=lambda hand: (hand_key(hand) != actual_key, hand_key(hand)))
    seconds_left = max(0.05, deadline - time.perf_counter())
    cap = max(24, min(SUBGAME_HAND_CAP, int(36 + seconds_left * 24)))
    if len(all_hands) <= cap:
        return all_hands

    rng = random.Random(actual_key)
    kept = [list(actual_hand)]
    remaining = [hand for hand in all_hands if hand_key(hand) != actual_key]
    kept.extend(rng.sample(remaining, cap - 1))
    return kept


def context_for_private_hand(context, private_hand, ranges):
    opponents = context["opponents"]
    range_factor = opponent_range_factor(private_hand, context["board"], ranges)
    equity_samples = 6 if context["phase"] == "preflop" else 4
    equity = estimate_equity(private_hand, context["board"], opponents, samples=equity_samples)
    equity = max(0.01, min(0.99, equity * range_factor))
    updated = dict(context)
    updated["hand"] = private_hand
    updated["equity"] = equity
    updated["made_rank"] = best_rank(private_hand + context["board"]) if len(private_hand) + len(context["board"]) >= 5 else 0
    updated["draw_score"] = draw_potential(private_hand, context["board"])
    return updated


def opponent_range_factor(private_hand, board, ranges):
    if not ranges:
        return 1.0
    hero_strength = preflop_strength(private_hand) if not board else made_hand_score(private_hand, board)
    factor = 1.0
    for range_info in ranges.values():
        tightness = range_info["tightness"]
        aggression = range_info["aggression"]
        factor *= max(0.62, min(1.35, 1.12 - (tightness - 1.0) * 0.18 + hero_strength * 0.16))
        if aggression > 1.3 and hero_strength < 0.45:
            factor *= 0.88
    return max(0.42, min(1.45, factor))


def estimate_action_ev(context, candidate):
    action_type = candidate["type"]
    pot = context["pot"]
    to_call = context["to_call"]
    equity = context["equity"]
    opponents = context["opponents"]

    if action_type == "fold":
        return -to_call
    if action_type == "check":
        realization = equity_realization(context, aggression=False)
        return realization * pot - (1 - realization) * 0.10 * pot
    if action_type == "call":
        call_amount = min(to_call, context["stack"])
        future_penalty = 0.05 * pot * max(0, opponents - 1)
        return equity_realization(context, aggression=False) * (pot + call_amount) - call_amount - future_penalty

    if action_type in {"bet", "raise"}:
        invest = max(0, int(candidate["total"]) - context["bet_round"])
    else:
        invest = context["stack"]

    invest = min(context["stack"], invest)
    final_pot = pot + invest + (to_call if action_type == "raise" else 0)
    fold_equity = estimate_fold_equity(context, candidate, invest)
    showdown_equity = equity_realization(context, aggression=True)
    called_ev = showdown_equity * final_pot - invest
    return fold_equity * pot + (1.0 - fold_equity) * called_ev


def equity_realization(context, aggression):
    equity = context["equity"]
    if context["phase"] == "preflop":
        factor = 0.82 if context["opponents"] >= 3 else 0.90
    else:
        factor = 0.88 + context["draw_score"] * 0.12
    if aggression:
        factor += 0.08
    if context["position"] in {"button", "late"}:
        factor += 0.04
    return max(0.0, min(0.98, equity * factor))


def estimate_fold_equity(context, candidate, invest):
    if candidate["type"] == "all-in":
        ratio = 1.4
    else:
        ratio = invest / max(1, context["pot"] + context["to_call"])
    base = 0.10 + min(0.45, ratio * 0.28)
    if context["phase"] == "preflop" and candidate["type"] == "all-in":
        # Preflop jams get called far more often than this toy subgame can infer,
        # especially deep and multiway. Keep fold equity conservative.
        base *= 0.22 if context["stack_bb"] > 35 else 0.45 if context["stack_bb"] > 12 else 0.75
    if context["active_players"] >= 4:
        base *= 0.65
    if context["texture"] in {"very_wet", "paired"}:
        base *= 0.85
    if context["phase"] == "river":
        base += 0.05
    if context["equity"] > 0.72:
        base *= 0.75
    return max(0.02, min(0.62, base))


def materialize(candidate):
    action_type = candidate["type"]
    if action_type in {"bet", "raise"}:
        return {"type": action_type, "amount": candidate["amount"]}
    if candidate.get("amount") is not None:
        return {"type": action_type, "amount": candidate["amount"]}
    return {"type": action_type}


def candidate_key(candidate):
    return f"{candidate['type']}:{candidate.get('size', '')}:{candidate.get('total', '')}"


def hand_key(hand):
    return " ".join(sorted(hand))


def normalize(weights):
    total = sum(max(0.0, value) for value in weights.values())
    if total <= 0:
        return {key: 1.0 / len(weights) for key in weights}
    return {key: max(0.0, value) / total for key, value in weights.items()}


def weighted_choice(candidates, weights):
    weights = normalize(weights)
    total = sum(weights.get(candidate_key(candidate), 0.0) for candidate in candidates)
    if total <= 0:
        return random.choice(candidates)
    threshold = random.random() * total
    cumulative = 0.0
    for candidate in candidates:
        cumulative += weights.get(candidate_key(candidate), 0.0)
        if threshold <= cumulative:
            return candidate
    return candidates[-1]


def estimate_equity(hand, board, opponents, samples=70):
    used = set(hand + board)
    deck = [card for card in make_deck() if card not in used]
    board_needed = 5 - len(board)
    wins = 0.0
    iterations = max(1, samples)
    for _ in range(iterations):
        draw = random.sample(deck, board_needed + 2 * opponents)
        runout = board + draw[:board_needed]
        offset = board_needed
        hero_rank = evaluate_best(hand + runout)
        ranks = [hero_rank]
        for _opponent in range(opponents):
            villain = draw[offset : offset + 2]
            offset += 2
            ranks.append(evaluate_best(villain + runout))
        best = max(ranks)
        winners = [rank for rank in ranks if rank == best]
        if hero_rank == best:
            wins += 1.0 / len(winners)
    return wins / iterations


def preflop_strength(hand):
    color = infer_hand_color("", hand)
    index = COLOR_TO_INDEX.get(color, 0)
    max_index = max(1, len(COLOR_TO_INDEX) - 1)
    return max(0.05, min(0.95, 0.08 + 0.87 * (index / max_index)))


def made_hand_score(hand, board):
    rank = best_rank(hand + board)
    if rank >= 5:
        return 0.92
    if rank >= 2:
        return 0.74
    if rank == 1:
        return 0.54
    return 0.28 + draw_potential(hand, board) * 0.45


def make_deck():
    return [f"{rank}{suit}" for suit in SUITS for rank in RANKS]


def evaluate_best(cards):
    return max(evaluate_five(combo) for combo in itertools.combinations(cards, 5))


def best_rank(cards):
    if len(cards) < 5:
        return 0
    return evaluate_best(cards)[0]


def evaluate_five(cards):
    ranks = sorted((VALUES[card[0]] for card in cards), reverse=True)
    suits = [card[1] for card in cards]
    counts = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    pattern = sorted(counts.values(), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    straight_high = straight_value(unique)
    flush = len(set(suits)) == 1
    ordered = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    ordered_ranks = [rank for rank, _count in ordered]
    if straight_high and flush:
        return 8, [straight_high]
    if pattern == [4, 1]:
        return 7, ordered_ranks
    if pattern == [3, 2]:
        return 6, ordered_ranks
    if flush:
        return 5, ranks
    if straight_high:
        return 4, [straight_high]
    if pattern == [3, 1, 1]:
        return 3, ordered_ranks
    if pattern == [2, 2, 1]:
        return 2, ordered_ranks
    if pattern == [2, 1, 1, 1]:
        return 1, ordered_ranks
    return 0, ranks


def straight_value(unique):
    if len(unique) < 5:
        return 0
    if unique[0] - unique[4] == 4:
        return unique[0]
    if unique[:4] == [14, 5, 4, 3] and 2 in unique:
        return 5
    return 0


def draw_potential(hand, board):
    if not board:
        return 0.0
    cards = hand + board
    score = 0.0
    if has_flush_draw(cards):
        score += 0.35
    if has_straight_draw(cards):
        score += 0.30
    if overcards(hand, board):
        score += 0.12
    return min(0.75, score)


def has_flush_draw(cards):
    suits = {}
    for card in cards:
        suits[card[1]] = suits.get(card[1], 0) + 1
    return max(suits.values(), default=0) >= 4


def has_straight_draw(cards):
    values = sorted({VALUES[card[0]] for card in cards})
    if 14 in values:
        values = [1] + values
    best = 1
    run = 1
    for index in range(1, len(values)):
        if values[index] == values[index - 1] + 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best >= 4


def overcards(hand, board):
    if not board:
        return False
    board_high = max(VALUES[card[0]] for card in board)
    return sum(1 for card in hand if VALUES[card[0]] > board_high) >= 1


def board_texture(board):
    if len(board) < 3:
        return "na"
    suits = [card[1] for card in board]
    paired = len({card[0] for card in board}) < len(board)
    values = sorted({VALUES[card[0]] for card in board})
    if 14 in values:
        values = [1] + values
    longest = 1
    run = 1
    for index in range(1, len(values)):
        if values[index] == values[index - 1] + 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    suit_count = max(suits.count(suit) for suit in set(suits))
    if paired:
        return "paired"
    if suit_count >= 3 or longest >= 4:
        return "very_wet"
    if suit_count == 2:
        return "two_tone"
    if longest >= 3:
        return "connected"
    return "dry"


def classify_position(game_state, player_state):
    players = game_state.get("players", [])
    total = max(2, len(players))
    dealer = int(game_state.get("dealer_index", 0))
    seat = int(player_state.get("seat", 0))
    if total == 2:
        return "button" if seat == dealer else "big_blind"
    offset = (seat - dealer) % total
    if offset == 0:
        return "button"
    if offset in {1, 2}:
        return "blind"
    if offset >= total - 2:
        return "late"
    if total >= 6 and offset >= 3:
        return "middle"
    return "early"


def classify_pressure(to_call, pot, stack):
    if to_call <= 0:
        return "none"
    if to_call / max(1, stack) >= 0.55:
        return "jam"
    ratio = to_call / max(1, pot)
    if ratio <= 0.25:
        return "small"
    if ratio <= 0.70:
        return "medium"
    return "large"


def round_to_unit(value, unit=25):
    return int(round(float(value) / unit) * unit)
