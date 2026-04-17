from __future__ import annotations

"""学習済み戦略表とプリフロップ土台戦略をブレンドした完成版 JSON を作ります。"""

import argparse
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

import sys

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.strategy_tables.preflop_blueprint import blend_with_blueprint


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blend a learned strategy table with the preflop blueprint."
    )
    parser.add_argument("--in", dest="input_path", required=True, help="Input strategy table JSON")
    parser.add_argument("--out", dest="output_path", required=True, help="Output blended JSON")
    parser.add_argument(
        "--table-weight",
        type=float,
        default=0.82,
        help="How much to trust the learned table when blending preflop entries.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_path).expanduser().resolve()
    output_path = Path(args.output_path).expanduser().resolve()
    table = json.loads(input_path.read_text(encoding="utf-8"))

    blended = {}
    for infoset, strategy in table.items():
        legal_actions = [{"type": action} for action in strategy.keys()]
        blended[infoset] = blend_with_blueprint(
            strategy,
            infoset,
            legal_actions,
            table_weight=args.table_weight,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(blended, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "input": str(input_path),
                "output": str(output_path),
                "entries": len(blended),
                "table_weight": args.table_weight,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
