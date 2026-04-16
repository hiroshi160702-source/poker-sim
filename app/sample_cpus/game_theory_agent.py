import random

# これは理論寄りの近似 CPU です。pot odds、MDF 風の防衛、ブラフ頻度、
# サイズの混合などを、厳密求解なしでヒューリスティックに近似しています。
RANKS = "23456789TJQKA"
VALUES = {rank: index for index, rank in enumerate(RANKS, start=2)}


def decide_action(game_state, player_state, legal_actions):
    hand = player_state["actual_hand"]
    board = game_state["community_cards"]
    strength = estimate_strength(hand, board)
    to_call = max(0, game_state["current_bet"] - player_state["bet_round"])
    pot = max(1, game_state["pot"])
    stack = max(1, player_state["stack"])
    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    mdf = pot / (pot + max(to_call, 1)) if to_call > 0 else 1.0
    aggression = raise_pressure(game_state, player_state)

    # ブラフ頻度とバリュー頻度は、手の強さと現在の圧力から決めます。
    value_threshold = 0.72 - aggression * 0.06
    thin_value_threshold = 0.58 - aggression * 0.04
    bluff_threshold = 0.30 - aggression * 0.03

    if board:
        draw_bonus = draw_potential(hand, board)
        strength = min(0.99, strength + draw_bonus)

    if to_call > 0:
        # ベットに直面しているときは、まず pot odds と比較してから、
        # raise / call / fold のどの領域に入るかを決めます。
        if strength + 0.03 >= pot_odds:
            raise_action = first_action(legal_actions, {"raise"})
            call_action = first_action(legal_actions, {"call"})
            all_in_action = first_action(legal_actions, {"all-in"})

            if raise_action and strength >= value_threshold and random.random() < mix_frequency(strength, "value"):
                return sized_raise(raise_action, strength, pot, stack)
            if raise_action and bluff_candidate(hand, board) and strength <= bluff_threshold and random.random() < bluff_frequency(aggression):
                return sized_raise(raise_action, 0.35, pot, stack)
            if call_action:
                return {"type": "call", "amount": call_action["amount"]}
            if all_in_action and strength >= 0.84:
                return {"type": "all-in", "amount": all_in_action["amount"]}

        # MDF 風の補助として、レンジ上位ハンドは一定頻度で防衛します。
        if random.random() < mdf and strength >= pot_odds * 0.82:
            call_action = first_action(legal_actions, {"call"})
            if call_action:
                return {"type": "call", "amount": call_action["amount"]}

        fold_action = first_action(legal_actions, {"fold"})
        if fold_action:
            return {"type": "fold"}

    bet_action = first_action(legal_actions, {"bet"})
    raise_action = first_action(legal_actions, {"raise"})
    check_action = first_action(legal_actions, {"check"})
    all_in_action = first_action(legal_actions, {"all-in"})

    if bet_action and strength >= value_threshold and random.random() < mix_frequency(strength, "value"):
        return sized_raise(bet_action, strength, pot, stack)
    if bet_action and bluff_candidate(hand, board) and random.random() < bluff_frequency(aggression) * 0.8:
        return sized_raise(bet_action, 0.32, pot, stack)
    if raise_action and strength >= thin_value_threshold and random.random() < mix_frequency(strength, "thin"):
        return sized_raise(raise_action, strength, pot, stack)
    if all_in_action and strength >= 0.9:
        return {"type": "all-in", "amount": all_in_action["amount"]}
    if check_action:
        return {"type": "check"}

    fallback = legal_actions[0]
    return {"type": fallback["type"], "amount": fallback.get("amount")}


def first_action(legal_actions, action_types):
    for action in legal_actions:
        if action["type"] in action_types:
            return action
    return None


def mix_frequency(strength, mode):
    if mode == "value":
        return min(0.95, max(0.35, (strength - 0.45) / 0.5))
    if mode == "thin":
        return min(0.8, max(0.2, (strength - 0.35) / 0.55))
    return 0.5


def bluff_frequency(aggression):
    return max(0.08, 0.28 - aggression * 0.05)


def sized_raise(action, strength, pot, stack):
    min_total = action["min_total"]
    max_total = action["max_total"]
    target = min_total

    # サイズを 1 種類に固定せず、いくつか混ぜて戦略を単調にしません。
    if strength >= 0.82:
        target = min_total + int((max_total - min_total) * 0.7)
    elif strength >= 0.65:
        target = min_total + int((max_total - min_total) * 0.45)
    elif strength >= 0.35:
        target = min_total + int((max_total - min_total) * 0.2)

    # ディープ時に薄いバリューで極端に大きく張りすぎないように制限します。
    cap = min(max_total, min_total + pot + int(stack * 0.35))
    target = max(min_total, min(target, cap))
    return {"type": action["type"], "amount": target}


def raise_pressure(game_state, player_state):
    to_call = max(0, game_state["current_bet"] - player_state["bet_round"])
    pot = max(1, game_state["pot"])
    return min(1.5, to_call / pot)


def bluff_candidate(hand, board):
    if not board:
        return preflop_bluff_candidate(hand)
    return has_flush_draw(hand + board) or has_straight_draw(hand + board) or blocker_heavy(hand, board)


def preflop_bluff_candidate(hand):
    a, b = hand
    vals = sorted([VALUES[a[0]], VALUES[b[0]]], reverse=True)
    suited = a[1] == b[1]
    return suited and vals[0] >= 10 and vals[1] >= 5


def blocker_heavy(hand, board):
    board_ranks = {card[0] for card in board}
    return hand[0][0] == "A" or hand[1][0] == "A" or hand[0][0] in board_ranks or hand[1][0] in board_ranks


def estimate_strength(hand, board):
    if not board:
        return preflop_strength(hand)

    rank = best_rank(hand + board)
    if rank >= 6:
        return 0.95
    if rank == 5:
        return 0.88
    if rank == 4:
        return 0.84
    if rank == 3:
        return 0.78
    if rank == 2:
        return 0.72
    if rank == 1:
        board_high = max((VALUES[card[0]] for card in board), default=0)
        hole_high = max(VALUES[card[0]] for card in hand)
        return 0.63 if hole_high >= board_high else 0.48
    return 0.26


def preflop_strength(hand):
    a, b = hand
    suited = a[1] == b[1]
    vals = sorted([VALUES[a[0]], VALUES[b[0]]], reverse=True)
    pair = vals[0] == vals[1]
    gap = vals[0] - vals[1]

    if pair and vals[0] >= 11:
        return 0.9
    if pair and vals[0] >= 8:
        return 0.78
    if pair:
        return 0.62
    if vals[0] >= 14 and vals[1] >= 12:
        return 0.74
    if suited and gap <= 1 and vals[0] >= 10:
        return 0.7
    if suited and gap <= 2 and vals[0] >= 8:
        return 0.56
    if vals[0] >= 13 and vals[1] >= 10:
        return 0.58
    return 0.32


def best_rank(cards):
    best = 0
    for combo in combinations(cards, 5):
        best = max(best, evaluate_five(combo)[0])
    return best


def combinations(cards, size):
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


def evaluate_five(cards):
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


def is_straight(ranks):
    unique = sorted(set(ranks), reverse=True)
    if len(unique) != 5:
        return False
    if unique[0] - unique[-1] == 4:
        return True
    return unique == [14, 5, 4, 3, 2]


def has_flush_draw(cards):
    suits = {}
    for card in cards:
        suits[card[1]] = suits.get(card[1], 0) + 1
    return max(suits.values()) >= 4


def has_straight_draw(cards):
    values = sorted({VALUES[card[0]] for card in cards})
    if 14 in values:
        values = [1] + values
    run = 1
    best = 1
    for index in range(1, len(values)):
        if values[index] == values[index - 1] + 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best >= 4


def draw_potential(hand, board):
    cards = hand + board
    bonus = 0.0
    if has_flush_draw(cards):
        bonus += 0.12
    if has_straight_draw(cards):
        bonus += 0.1
    return bonus
