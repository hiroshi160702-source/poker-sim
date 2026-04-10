from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, List


DecisionFn = Callable[[dict, dict, List[dict]], dict]


class CpuAgentError(Exception):
    """Raised when an external CPU module cannot be loaded or used."""


class CpuLoader:
    def __init__(self) -> None:
        self._cache: Dict[str, DecisionFn] = {}

    def load(self, path: str) -> DecisionFn:
        resolved = str(Path(path).expanduser().resolve())
        if resolved in self._cache:
            return self._cache[resolved]

        module = self._load_module(resolved)
        decide_action = getattr(module, "decide_action", None)
        if not callable(decide_action):
            raise CpuAgentError(
                f"{resolved} には callable な decide_action(game_state, player_state, legal_actions) が必要です。"
            )

        self._cache[resolved] = decide_action
        return decide_action

    def clear_cache(self, path: str) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self._cache.pop(resolved, None)

    def _load_module(self, resolved_path: str) -> ModuleType:
        module_name = f"cpu_agent_{abs(hash(resolved_path))}"
        spec = importlib.util.spec_from_file_location(module_name, resolved_path)
        if spec is None or spec.loader is None:
            raise CpuAgentError(f"CPUファイルを読み込めませんでした: {resolved_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
