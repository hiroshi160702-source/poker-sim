from __future__ import annotations

"""Multi-player 向けの学習済み戦略表を読む CPU です。"""

from pathlib import Path

from app.sample_cpus import strategy_table_cpu as base_cpu
from app.strategy_tables.preflop_blueprint import blend_with_blueprint

DEFAULT_TABLE_CANDIDATES = [
    Path(__file__).resolve().parent
    / "strategy_tables"
    / "mllui_player_preflop_mccfr_6p_500000.json",
    Path(__file__).resolve().parent
    / "strategy_tables"
    / "mllui_player_preflop_mccfr_6p_300000.json",
    Path(__file__).resolve().parent
    / "strategy_tables"
    / "mllui_player_preflop_mccfr_6p_100000.json",
    Path(__file__).resolve().parent
    / "strategy_tables"
    / "multiplayer_strategy_6p_5000000hands.json",
]


def decide_action(game_state, player_state, legal_actions):
    table = load_strategy_table()
    infoset = base_cpu.encode_infoset(game_state, player_state)
    strategy = base_cpu.lookup_strategy(table, infoset, legal_actions)
    strategy = blend_with_blueprint(
        strategy,
        infoset,
        legal_actions,
        table_weight=0.8,
        game_state=game_state,
        player_state=player_state,
    )
    strategy = base_cpu.apply_safety_overrides(strategy, infoset, legal_actions)
    action_type = base_cpu.sample_action(strategy)
    return base_cpu.materialize_action(action_type, legal_actions, infoset, game_state, player_state)


def load_strategy_table(table_path: str | None = None):
    if table_path:
        return base_cpu.load_strategy_table(table_path)
    for candidate in DEFAULT_TABLE_CANDIDATES:
        if candidate.exists():
            return base_cpu.load_strategy_table(str(candidate))
    return base_cpu.load_strategy_table()
