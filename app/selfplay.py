from __future__ import annotations

"""CPU 自己対戦の実行と戦略表の書き出しを担当する補助関数群です。"""

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, Optional

from .engine import CPU_NAME_POOL, HoldemGame
from .strategy_tables.lib import candidate_infosets, encode_infoset


def run_heads_up_cpu_match(
    logs_dir: Path,
    embedded_cpu_dir: Path,
    hero_cpu_path: str,
    villain_cpu_path: str,
    hands: int,
    starting_stack: int,
    export_strategy_path: Optional[str] = None,
) -> dict:
    # 旧来のヘッズアップ用補助ですが、CLI の戦略表生成ではまだ使っています。
    game = HoldemGame(logs_dir, embedded_cpu_dir)
    game.configure_table(starting_stack=starting_stack, cpu_count=1)

    # 自己対戦では 0 番席も CPU 化します。
    game.players[0].is_human = False
    game.players[0].cpu_path = str(Path(hero_cpu_path).expanduser().resolve())
    game.cpu_loader.load(game.players[0].cpu_path)
    game.load_cpu(1, villain_cpu_path)

    stats = {
        "hands": hands,
        "hero_name": "CPU Hero",
        "villain_name": game.players[1].name,
        "hero_path": game.players[0].cpu_path,
        "villain_path": game.players[1].cpu_path,
        "hero_wins": 0,
        "villain_wins": 0,
        "draws": 0,
        "hero_profit": 0,
        "villain_profit": 0,
        "recent_results": [],
        "visited_infosets": 0,
        "phase_breakdown": {},
    }
    action_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    infoset_visits: Dict[str, int] = defaultdict(int)
    phase_visits: Dict[str, int] = defaultdict(int)
    initial_stack = starting_stack

    for hand_index in range(hands):
        # autoplay_cpus=False にして、エンジンが自動進行する前に各 CPU の
        # 明示的な意思決定を記録できるようにします。
        game.start_new_hand(autoplay_cpus=False)
        safety = 0
        while not game.awaiting_new_hand and safety < 200:
            safety += 1
            if game.current_turn is None:
                break
            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break

            infoset = encode_infoset(game.serialize_for_cpu(), player.to_public_dict(True))
            infoset_visits[infoset] += 1
            phase_visits[infoset.split("|")[0]] += 1
            decision = game.cpu_decision_for(player, legal_actions)
            action_counts[infoset][decision.get("type", "fold")] += 1

            try:
                game.apply_player_action(seat, decision.get("type", ""), decision.get("amount"))
            except Exception:
                fallback = game.fallback_decision(legal_actions)
                action_counts[infoset][fallback["type"]] += 1
                game.apply_player_action(seat, fallback["type"], fallback.get("amount"))

        hero_result = result_for_seat(game.last_winners, 0)
        villain_result = result_for_seat(game.last_winners, 1)
        stats["hero_profit"] += hero_result
        stats["villain_profit"] += villain_result

        if hero_result > villain_result:
            stats["hero_wins"] += 1
        elif villain_result > hero_result:
            stats["villain_wins"] += 1
        else:
            stats["draws"] += 1

        stats["recent_results"].insert(
            0,
            {
                "hand_id": game.hand_id,
                "hero_delta": hero_result,
                "villain_delta": villain_result,
                "message": game.table_message,
            },
        )
        stats["recent_results"] = stats["recent_results"][:15]

        # 長い対戦を止めないため、誰かが飛んだらスタックだけ初期値に戻します。
        if game.players[0].stack == 0 or game.players[1].stack == 0:
            game.players[0].stack = initial_stack
            game.players[1].stack = initial_stack
            game.awaiting_new_hand = True

    stats["visited_infosets"] = len(infoset_visits)
    stats["phase_breakdown"] = dict(sorted(phase_visits.items()))

    if export_strategy_path:
        strategy_table = to_probability_table(action_counts, infoset_visits)
        export_path = Path(export_strategy_path).expanduser().resolve()
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(
            json.dumps(strategy_table, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        stats["exported_strategy_path"] = str(export_path)
        stats["strategy_table"] = strategy_table
        stats["strategy_table_filename"] = export_path.name

    return stats


def run_multiway_cpu_match(
    logs_dir: Path,
    embedded_cpu_dir: Path,
    cpu_paths: list[str],
    hands: int,
    starting_stack: int,
    export_strategy_path: Optional[str] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    capture_replay: bool = False,
) -> dict:
    # 多人数自己対戦は専用の独立した卓で回し、ブラウザ上の対局状態とは
    # 完全に切り離して扱います。
    if not 2 <= len(cpu_paths) <= 9:
        raise ValueError("CPU paths must contain between 2 and 9 players.")

    resolved_paths = [str(Path(path).expanduser().resolve()) for path in cpu_paths]

    game = HoldemGame(logs_dir, embedded_cpu_dir)
    game.configure_table(starting_stack=starting_stack, cpu_count=len(resolved_paths) - 1)

    for seat, path in enumerate(resolved_paths):
        player = game.players[seat]
        player.is_human = False
        player.cpu_path = path
        player.name = f"CPU {seat}" if seat == 0 else CPU_NAME_POOL[seat - 1]
        game.cpu_loader.load(path)
        if seat > 0:
            game.load_cpu(seat, path)

    stats = {
        "hands": hands,
        "player_count": len(resolved_paths),
        "players": [
            {
                "seat": seat,
                "name": game.players[seat].name,
                "cpu_path": resolved_paths[seat],
                "wins": 0,
                "profit": 0,
                "first_places": 0,
                "avg_profit": 0.0,
            }
            for seat in range(len(resolved_paths))
        ],
        "recent_results": [],
        "visited_infosets": 0,
        "phase_breakdown": {},
        "seat_stats": [],
        "last_replay_snapshot": None,
    }
    action_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    infoset_visits: Dict[str, int] = defaultdict(int)
    phase_visits: Dict[str, int] = defaultdict(int)
    initial_stack = starting_stack
    started_at = time.perf_counter()

    if progress_callback:
        # 最初の進捗を先に返して、UI をすぐ「実行中」表示へ切り替えます。
        progress_callback(
            {
                "completed_hands": 0,
                "total_hands": hands,
                "percent": 0.0,
                "message": "CPU self-play is starting.",
                "latest_snapshot": None,
                "elapsed_seconds": 0.0,
                "estimated_remaining_seconds": None,
            }
        )

    for _hand_index in range(hands):
        game.start_new_hand(autoplay_cpus=False)
        safety = 0
        while not game.awaiting_new_hand and safety < 400:
            safety += 1
            if game.current_turn is None:
                break
            seat = game.current_turn
            player = game.players[seat]
            legal_actions = game.legal_actions_for(seat)
            if not legal_actions:
                break

            infoset = encode_infoset(game.serialize_for_cpu(), player.to_public_dict(True))
            infoset_visits[infoset] += 1
            phase_visits[infoset.split("|")[0]] += 1
            decision = game.cpu_decision_for(player, legal_actions)
            action_counts[infoset][decision.get("type", "fold")] += 1

            try:
                game.apply_player_action(seat, decision.get("type", ""), decision.get("amount"))
            except Exception:
                fallback = game.fallback_decision(legal_actions)
                action_counts[infoset][fallback["type"]] += 1
                game.apply_player_action(seat, fallback["type"], fallback.get("amount"))

        winners_by_seat = {winner["seat"]: winner["amount"] for winner in game.last_winners}
        result_players = []
        hand_deltas = []
        for player_stats in stats["players"]:
            seat = player_stats["seat"]
            amount = winners_by_seat.get(seat, 0)
            player_stats["profit"] += amount
            if amount > 0:
                player_stats["wins"] += 1
            result_players.append({"seat": seat, "name": player_stats["name"], "delta": amount})
            hand_deltas.append((seat, amount))

        best_delta = max((delta for _, delta in hand_deltas), default=0)
        if best_delta > 0:
            for seat, delta in hand_deltas:
                if delta == best_delta:
                    stats["players"][seat]["first_places"] += 1

        stats["recent_results"].insert(
            0,
            {
                "hand_id": game.hand_id,
                "players": result_players,
                "message": game.table_message,
            },
        )
        stats["recent_results"] = stats["recent_results"][:15]

        busted = [player for player in game.players[: len(resolved_paths)] if player.stack == 0]
        if busted:
            for player in game.players[: len(resolved_paths)]:
                player.stack = initial_stack
            game.awaiting_new_hand = True

        latest_snapshot = None
        if capture_replay:
            # リプレイ用スナップショットは軽量に保ち、各ハンド終了時点の状態だけ
            # を持つことでメモリ消費を増やしすぎないようにします。
            latest_snapshot = build_replay_snapshot(game)
            stats["last_replay_snapshot"] = latest_snapshot

        should_report_progress = False
        if progress_callback:
            completed_hands = _hand_index + 1
            update_interval = 1000
            elapsed_seconds = max(0.0, time.perf_counter() - started_at)
            hands_per_second = completed_hands / elapsed_seconds if elapsed_seconds > 0 else 0.0
            remaining_hands = max(0, hands - completed_hands)
            estimated_remaining_seconds = (
                remaining_hands / hands_per_second if hands_per_second > 0 else None
            )
            should_report_progress = (
                capture_replay
                or completed_hands == 1
                or completed_hands == hands
                or completed_hands % update_interval == 0
            )
        if should_report_progress:
            progress_callback(
                {
                    "completed_hands": completed_hands,
                    "total_hands": hands,
                    "percent": round((completed_hands / max(1, hands)) * 100, 1),
                    "message": f"Simulated {completed_hands} / {hands} hands.",
                    "latest_snapshot": latest_snapshot,
                    "leaderboard_preview": build_leaderboard_preview(stats["players"], completed_hands),
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "estimated_remaining_seconds": (
                        round(estimated_remaining_seconds, 1)
                        if estimated_remaining_seconds is not None
                        else None
                    ),
                }
            )

    stats["visited_infosets"] = len(infoset_visits)
    stats["phase_breakdown"] = dict(sorted(phase_visits.items()))
    for player in stats["players"]:
        player["avg_profit"] = round(player["profit"] / max(1, hands), 2)
        player["first_place_rate"] = round(player["first_places"] / max(1, hands) * 100, 2)
    stats["leaderboard"] = sorted(
        (
            {
                "seat": player["seat"],
                "name": player["name"],
                "wins": player["wins"],
                "profit": player["profit"],
                "avg_profit": player["avg_profit"],
                "first_places": player["first_places"],
                "first_place_rate": player["first_place_rate"],
                "cpu_path": player["cpu_path"],
            }
            for player in stats["players"]
        ),
        key=lambda player: (player["profit"], player["first_places"], player["wins"]),
        reverse=True,
    )
    stats["seat_stats"] = [
        {
            "seat": player["seat"],
            "name": player["name"],
            "wins": player["wins"],
            "first_places": player["first_places"],
            "first_place_rate": player["first_place_rate"],
            "avg_profit": player["avg_profit"],
            "profit": player["profit"],
        }
        for player in sorted(stats["players"], key=lambda player: player["seat"])
    ]

    strategy_table = to_probability_table(action_counts, infoset_visits)
    stats["strategy_table"] = strategy_table
    stats["elapsed_seconds"] = round(max(0.0, time.perf_counter() - started_at), 1)
    stats["strategy_table_filename"] = (
        Path(export_strategy_path).expanduser().resolve().name
        if export_strategy_path
        else f"multiplayer_strategy_{len(resolved_paths)}p_{hands}hands.json"
    )

    if export_strategy_path:
        export_path = Path(export_strategy_path).expanduser().resolve()
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(
            json.dumps(strategy_table, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        stats["exported_strategy_path"] = str(export_path)

    return stats


def build_leaderboard_preview(players: list[dict], completed_hands: int) -> list[dict]:
    preview = []
    for player in sorted(players, key=lambda entry: (entry["profit"], entry["wins"]), reverse=True)[:3]:
        preview.append(
            {
                "name": player["name"],
                "profit": player["profit"],
                "wins": player["wins"],
                "avg_profit": round(player["profit"] / max(1, completed_hands), 2),
            }
        )
    return preview


def build_replay_snapshot(game: HoldemGame) -> dict:
    state = game.serialize_state(reveal_all_cards=True, reveal_folded=True)
    return {
        "hand_id": state["hand_id"],
        "phase": state["phase"],
        "pot": state["pot"],
        "community_cards": state["community_cards"],
        "table_message": state["table_message"],
        "players": [
            {
                "seat": player["seat"],
                "name": player["name"],
                "stack": player["stack"],
                "hand": player["hand"],
                "last_action": player["last_action"],
                "folded": player["folded"],
                "in_hand": player["in_hand"],
                "all_in": player["all_in"],
                "win_amount": player["win_amount"],
            }
            for player in state["players"]
        ],
        "last_winners": state["last_winners"],
    }


def result_for_seat(winners: list[dict], seat: int) -> int:
    for winner in winners:
        if winner["seat"] == seat:
            return winner["amount"]
    return 0


def to_probability_table(
    action_counts: Dict[str, Dict[str, int]],
    infoset_visits: Dict[str, int],
) -> dict:
    # 厳密な訪問回数に加えて、より広い親バケットの情報も混ぜることで、
    # 訪問数の少ない状態でも使える戦略表にします。
    generalized_counts = build_generalized_counts(action_counts)
    generalized_visits = build_generalized_visits(infoset_visits)
    table = {}

    all_keys = set(action_counts) | set(generalized_counts)
    for infoset in all_keys:
        counts = {}
        if infoset in generalized_counts and infoset not in action_counts:
            counts = dict(generalized_counts[infoset])
        else:
            counts = blend_counts(
                infoset,
                action_counts.get(infoset, {}),
                infoset_visits,
                generalized_counts,
                generalized_visits,
            )
        total = sum(counts.values())
        if total <= 0:
            continue
        table[infoset] = {
            action: round(value / total, 4) for action, value in sorted(counts.items())
        }
    return dict(sorted(table.items()))


def build_generalized_counts(action_counts: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, float]]:
    generalized: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for infoset, counts in action_counts.items():
        for generalized_key, weight in generalization_targets(infoset):
            for action, count in counts.items():
                generalized[generalized_key][action] += count * weight
    return generalized


def build_generalized_visits(infoset_visits: Dict[str, int]) -> Dict[str, float]:
    generalized: Dict[str, float] = defaultdict(float)
    for infoset, visits in infoset_visits.items():
        for generalized_key, weight in generalization_targets(infoset):
            generalized[generalized_key] += visits * weight
    return generalized


def generalization_targets(infoset: str) -> list[tuple[str, float]]:
    # 粗い一般化ほど重みを小さくして、十分にサンプルがある状態では
    # 厳密な観測結果が優先されるようにします。
    targets = []
    seen = {infoset}
    weights = {
        (1,): 0.42,
        (2,): 0.38,
        (3,): 0.52,
        (4,): 0.26,
        (5,): 0.24,
        (1, 3): 0.24,
        (2, 3): 0.22,
        (1, 2): 0.16,
        (4, 5): 0.16,
        (2, 5): 0.14,
        (1, 4): 0.12,
        (1, 2, 3): 0.09,
        (2, 4, 5): 0.08,
        (1, 2, 3, 4): 0.05,
        (1, 2, 3, 4, 5): 0.03,
    }
    for key in candidate_infosets(infoset)[1:]:
        if key not in seen:
            seen.add(key)
            parts = infoset.split("|")
            collapsed = tuple(index for index, value in enumerate(key.split("|")) if value == "any" and parts[index] != "any")
            weight = weights.get(collapsed)
            if weight:
                targets.append((key, weight))
    return targets


def blend_counts(
    infoset: str,
    exact_counts: Dict[str, int],
    infoset_visits: Dict[str, int],
    generalized_counts: Dict[str, Dict[str, float]],
    generalized_visits: Dict[str, float],
) -> Dict[str, float]:
    blended = defaultdict(float)
    exact_visits = infoset_visits.get(infoset, 0)
    for action, count in exact_counts.items():
        blended[action] += float(count)

    smoothing = max(0.0, 24.0 - exact_visits)
    if smoothing <= 0:
        return dict(blended)

    for generalized_key, base_weight in generalization_targets(infoset):
        parent_visits = generalized_visits.get(generalized_key, 0.0)
        parent_counts = generalized_counts.get(generalized_key)
        if not parent_counts or parent_visits <= 0:
            continue
        scale = smoothing * base_weight / parent_visits
        for action, count in parent_counts.items():
            blended[action] += count * scale
    return dict(blended)
