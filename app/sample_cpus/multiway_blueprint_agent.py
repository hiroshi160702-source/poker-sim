from __future__ import annotations

"""Multiway blueprint-only CPU.

This agent uses the learned multiway blueprint table directly. It intentionally
does not run Pluribus-style range updates, subgame CFR, or runtime safety
overrides, so it is useful for checking the raw blueprint policy.
"""

import random

from app.sample_cpus import pluribus_agent
from app.strategy_tables.lib import encode_infoset


def decide_action(game_state, player_state, legal_actions):
    context = build_light_context(game_state, player_state)
    candidates = pluribus_agent.build_action_candidates(legal_actions, context)
    if not candidates:
        return {"type": "fold"}

    infoset = encode_infoset(game_state, player_state)
    table = pluribus_agent.load_blueprint()
    strategy = pluribus_agent.lookup_blueprint_candidate_strategy(table, infoset, candidates)
    if not strategy:
        strategy = fallback_strategy(candidates)

    chosen = weighted_choice(candidates, strategy)
    return pluribus_agent.materialize(chosen)


def build_light_context(game_state, player_state):
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
    big_blind = max(1, int(game_state.get("big_blind", 50)))
    return {
        "phase": game_state.get("phase", "preflop"),
        "big_blind": big_blind,
        "pot": pot,
        "to_call": to_call,
        "stack": stack,
        "bet_round": bet_round,
        "opponents": opponents,
        "active_players": len(active),
        "effective_stack": effective_stack,
        "stack_bb": effective_stack / big_blind,
        "spr": effective_stack / pot,
        "pot_odds": to_call / (pot + to_call) if to_call > 0 else 0.0,
    }


def fallback_strategy(candidates):
    weights = {}
    for candidate in candidates:
        key = pluribus_agent.candidate_key(candidate)
        action_type = candidate["type"]
        if action_type in {"check", "call"}:
            weights[key] = 3.0
        elif action_type == "fold":
            weights[key] = 2.0
        elif action_type in {"bet", "raise"}:
            weights[key] = 1.0
        else:
            weights[key] = 0.2
    return pluribus_agent.normalize(weights)


def weighted_choice(candidates, strategy):
    threshold = random.random()
    cumulative = 0.0
    last = candidates[-1]
    for candidate in candidates:
        cumulative += strategy.get(pluribus_agent.candidate_key(candidate), 0.0)
        if threshold <= cumulative:
            return candidate
        last = candidate
    return last
