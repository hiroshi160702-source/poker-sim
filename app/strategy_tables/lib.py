from __future__ import annotations

"""戦略表参照型 CPU が共通利用する infoset 分類補助です。"""

RANKS = "23456789TJQKA"
VALUES = {rank: index for index, rank in enumerate(RANKS, start=2)}


def encode_infoset(game_state: dict, player_state: dict) -> str:
    # infoset のキーをコンパクトにして、戦略表ファイルを重くしすぎず、
    # それでも重要な局面差は残せるようにしています。
    phase = game_state["phase"]
    player_count = classify_table_participants(game_state)
    position = classify_position(game_state, player_state)
    pressure = classify_pressure(game_state, player_state)
    stack_bucket = classify_effective_stack(game_state, player_state)
    if phase == "preflop":
        bucket = classify_preflop(player_state["actual_hand"])
        texture = "na"
    else:
        bucket = classify_postflop(player_state["actual_hand"], game_state["community_cards"])
        texture = classify_board_texture(game_state["community_cards"])
    return "|".join([phase, player_count, position, bucket, pressure, stack_bucket, texture])


def collapse_infoset(infoset: str, index: int, replacement: str = "any") -> str:
    parts = infoset.split("|")
    if 0 <= index < len(parts):
        parts[index] = replacement
    return "|".join(parts)


def candidate_infosets(infoset: str) -> list[str]:
    # 戦略表は厳密一致から、より広い "any" バケットへ順にフォールバックし、
    # 疎な自己対戦データでも判断できるようにします。
    candidates = []
    seen = set()

    def add(key: str) -> None:
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    add(infoset)
    for index in (1, 2, 3, 4, 5, 6):
        add(collapse_infoset(infoset, index))
    for first, second in ((1, 3), (2, 4), (3, 4), (1, 2), (5, 6), (2, 5), (1, 5)):
        add(collapse_infoset(collapse_infoset(infoset, first), second))
    add(collapse_infoset(collapse_infoset(collapse_infoset(infoset, 1), 2), 3))
    add(collapse_infoset(collapse_infoset(collapse_infoset(infoset, 3), 5), 6))
    add(collapse_infoset(collapse_infoset(collapse_infoset(collapse_infoset(infoset, 1), 2), 3), 4))
    add(collapse_infoset(collapse_infoset(collapse_infoset(collapse_infoset(collapse_infoset(infoset, 2), 3), 4), 5), 6))
    return candidates


def classify_table_participants(game_state: dict) -> str:
    # 戦略表には卓人数を埋め込みますが、game_state のトップレベルへは
    # 専用キーを増やさず players 配列から直接数えます。
    active = [
        player
        for player in game_state["players"]
        if player.get("stack", 0) > 0
    ]
    return f"{len(active)}p"


def classify_position(game_state: dict, player_state: dict) -> str:
    # ポジション分類はあえて粗めです。卓人数が変わった場合やサンプル数が
    # 少ない場合でも一般化しやすくするためです。
    total = len(game_state["players"])
    dealer = game_state["dealer_index"]
    seat = player_state["seat"]
    if total == 2:
        return "button" if seat == dealer else "big_blind"
    offset = (seat - dealer) % total
    if offset == 0:
        return "button"
    if offset == 1:
        return "blind"
    if offset == 2:
        return "big_blind"
    if offset >= total - 2:
        return "late"
    if total >= 6 and offset >= 3:
        return "middle"
    return "early"


def classify_pressure(game_state: dict, player_state: dict) -> str:
    # pressure はスタックに対する危険度とポットオッズをまとめた分類で、
    # 軽いベットと実質コミット局面を分けられるようにしています。
    to_call = max(0, game_state["current_bet"] - player_state["bet_round"])
    if to_call == 0:
        return "none"
    pot = max(1, game_state.get("pot", 0))
    stack = max(1, player_state["stack"])
    stack_ratio = to_call / stack
    pot_ratio = to_call / pot
    if stack_ratio >= 0.6:
        return "jam"
    if stack_ratio <= 0.06 and pot_ratio <= 0.18:
        return "tiny"
    if stack_ratio <= 0.14 and pot_ratio <= 0.42:
        return "small"
    if stack_ratio <= 0.32:
        return "medium"
    return "large"


def classify_effective_stack(game_state: dict, player_state: dict) -> str:
    active_stacks = [
        contender["stack"]
        for contender in game_state["players"]
        if contender["seat"] != player_state["seat"] and contender["in_hand"] and not contender["folded"]
    ]
    effective = player_state["stack"] if not active_stacks else min(player_state["stack"], max(active_stacks))
    big_blind = 50
    stack_in_bb = effective / big_blind
    if stack_in_bb <= 12:
        return "shallow"
    if stack_in_bb <= 35:
        return "medium"
    if stack_in_bb <= 80:
        return "deep"
    return "very_deep"


def classify_preflop(hand: list[str]) -> str:
    card_a, card_b = hand
    suited = card_a[1] == card_b[1]
    values = sorted([VALUES[card_a[0]], VALUES[card_b[0]]], reverse=True)
    pair = values[0] == values[1]
    gap = values[0] - values[1]

    if pair and values[0] >= 11:
        return "premium"
    if pair and values[0] >= 8:
        return "strong"
    if pair:
        return "medium"
    if values[0] >= 13 and values[1] >= 11:
        return "strong"
    if suited and gap <= 2 and values[0] >= 8:
        return "speculative"
    if values[0] >= 11 and values[1] >= 9:
        return "medium"
    return "weak"


def classify_postflop(hand: list[str], board: list[str]) -> str:
    cards = hand + board
    rank = best_rank(cards)
    if rank >= 5:
        return "monster"
    if rank >= 2:
        return "made"
    if rank == 1:
        board_high = max((VALUES[card[0]] for card in board), default=0)
        hole_high = max(VALUES[card[0]] for card in hand)
        return "strong_pair" if hole_high >= board_high else "marginal"
    if has_flush_draw(cards) or has_straight_draw(cards):
        return "combo_draw" if has_flush_draw(cards) and has_straight_draw(cards) else "draw"
    return "air"


def classify_board_texture(board: list[str]) -> str:
    # ボードテクスチャはポストフロップだけで使い、多すぎる盤面形状を
    # 少数カテゴリへ潰して戦略表の爆発を防ぎます。
    if len(board) < 3:
        return "na"
    suits = [card[1] for card in board]
    paired = len({card[0] for card in board}) < len(board)
    values = sorted({VALUES[card[0]] for card in board})
    if 14 in values:
        values = [1] + values
    longest_run = 1
    run = 1
    for index in range(1, len(values)):
        if values[index] == values[index - 1] + 1:
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 1
    suit_count = max(suits.count(suit) for suit in set(suits))
    if paired:
        return "paired"
    if suit_count >= 3 or longest_run >= 4:
        return "very_wet"
    if suit_count == 2 and len(board) == 3:
        return "two_tone"
    if longest_run >= 3:
        return "connected"
    return "dry"


def best_rank(cards: list[str]) -> int:
    best = 0
    for combo in combinations(cards, 5):
        best = max(best, evaluate_five(combo)[0])
    return best


def combinations(cards: list[str], size: int) -> list[list[str]]:
    if size == 0:
        return [[]]
    if len(cards) < size:
        return []
    if len(cards) == size:
        return [cards]
    head = cards[0]
    with_head = [[head] + rest for rest in combinations(cards[1:], size - 1)]
    without_head = combinations(cards[1:], size)
    return with_head + without_head


def evaluate_five(cards: list[str]) -> tuple[int, list[int]]:
    ranks = sorted((VALUES[card[0]] for card in cards), reverse=True)
    suits = [card[1] for card in cards]
    counts = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    pattern = sorted(counts.values(), reverse=True)
    if is_straight(ranks) and len(set(suits)) == 1:
        return 8, ranks
    if pattern == [4, 1]:
        return 7, ranks
    if pattern == [3, 2]:
        return 6, ranks
    if len(set(suits)) == 1:
        return 5, ranks
    if is_straight(ranks):
        return 4, ranks
    if pattern == [3, 1, 1]:
        return 3, ranks
    if pattern == [2, 2, 1]:
        return 2, ranks
    if pattern == [2, 1, 1, 1]:
        return 1, ranks
    return 0, ranks


def is_straight(ranks: list[int]) -> bool:
    unique = sorted(set(ranks), reverse=True)
    if len(unique) != 5:
        return False
    if unique[0] - unique[-1] == 4:
        return True
    return unique == [14, 5, 4, 3, 2]


def has_flush_draw(cards: list[str]) -> bool:
    suits = {}
    for card in cards:
        suits[card[1]] = suits.get(card[1], 0) + 1
    return max(suits.values()) >= 4


def has_straight_draw(cards: list[str]) -> bool:
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
