from __future__ import annotations

"""White-box wrapper for pluribus_agent.py.

The decision itself is produced by the same Pluribus implementation. This file
only records the intermediate strategy, context, and explanation to JSONL so
self-play hands can be inspected afterwards.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from app.sample_cpus import pluribus_agent


DEFAULT_TRACE_PATH = Path("logs") / "pluribus_whitebox_decisions.jsonl"


def decide_action(game_state, player_state, legal_actions):
    decision, trace = pluribus_agent.decide_action_with_trace(
        game_state,
        player_state,
        legal_actions,
    )
    write_trace(trace)
    return decision


def write_trace(trace):
    path = Path(os.environ.get("PLURIBUS_TRACE_PATH", str(DEFAULT_TRACE_PATH))).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        **trace,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
