from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.selfplay import run_heads_up_cpu_match


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a strategy table from CPU self-play.")
    parser.add_argument("--hero", required=True, help="Python file for hero CPU")
    parser.add_argument("--villain", required=True, help="Python file for villain CPU")
    parser.add_argument("--hands", type=int, default=500, help="Number of heads-up hands")
    parser.add_argument("--stack", type=int, default=2000, help="Starting stack")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    result = run_heads_up_cpu_match(
        logs_dir=BASE_DIR / "logs",
        embedded_cpu_dir=BASE_DIR / "embedded_cpus",
        hero_cpu_path=args.hero,
        villain_cpu_path=args.villain,
        hands=args.hands,
        starting_stack=args.stack,
        export_strategy_path=args.out,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
