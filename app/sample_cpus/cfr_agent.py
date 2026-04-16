import random

# 本格的な CFR ソルバーではなく、現在局面を粗く抽象化して
# regret matching 風に行動を混ぜる軽量サンプルです。
RANKS = "23456789TJQKA"
VALUES = {rank: index for index, rank in enumerate(RANKS, start=2)}


REGRET_TABLE = {
    ("preflop", "premium", "any", "none"): {"raise": 28, "call": 5, "all-in": 2},
    ("preflop", "premium", "any", "small"): {"raise": 24, "call": 8, "all-in": 3},
    ("preflop", "premium", "any", "large"): {"call": 15, "raise": 12, "all-in": 6},
    ("preflop", "strong", "late", "none"): {"raise": 18, "call": 7, "check": 3},
    ("preflop", "strong", "blind", "small"): {"call": 13, "raise": 11},
    ("preflop", "strong", "any", "small"): {"call": 12, "raise": 8},
    ("preflop", "strong", "any", "large"): {"call": 14, "fold": 3, "raise": 5},
    ("preflop", "medium", "late", "none"): {"raise": 11, "call": 9, "check": 4},
    ("preflop", "medium", "blind", "small"): {"call": 12, "check": 5},
    ("preflop", "medium", "any", "small"): {"call": 10, "fold": 4},
    ("preflop", "medium", "any", "large"): {"fold": 14, "call": 6},
    ("preflop", "speculative", "late", "none"): {"raise": 7, "call": 9, "check": 4},
    ("preflop", "speculative", "any", "small"): {"call": 8, "fold": 6},
    ("preflop", "speculative", "any", "large"): {"fold": 15, "call": 2},
    ("preflop", "weak", "any", "none"): {"check": 9, "fold": 6},
    ("preflop", "weak", "any", "small"): {"fold": 15, "call": 1},
    ("preflop", "weak", "any", "large"): {"fold": 18},
    ("flop", "made", "any", "none"): {"bet": 16, "check": 4},
    ("flop", "made", "any", "small"): {"raise": 12, "call": 7},
    ("flop", "made", "any", "large"): {"call": 12, "raise": 8},
    ("flop", "draw", "late", "none"): {"bet": 10, "check": 7},
    ("flop", "draw", "any", "small"): {"call": 12, "raise": 5},
    ("flop", "draw", "any", "large"): {"call": 9, "fold": 5},
    ("flop", "marginal", "any", "none"): {"check": 12, "bet": 4},
    ("flop", "marginal", "any", "small"): {"call": 8, "fold": 5},
    ("flop", "marginal", "any", "large"): {"fold": 12, "call": 3},
    ("flop", "air", "any", "none"): {"check": 11, "bet": 3},
    ("flop", "air", "any", "small"): {"fold": 12, "call": 2},
    ("flop", "air", "any", "large"): {"fold": 16},
    ("turn", "made", "any", "none"): {"bet": 15, "check": 3},
    ("turn", "made", "any", "small"): {"raise": 10, "call": 8},
    ("turn", "made", "any", "large"): {"call": 11, "raise": 5, "fold": 1},
    ("turn", "draw", "any", "none"): {"check": 8, "bet": 5},
    ("turn", "draw", "any", "small"): {"call": 7, "fold": 4},
    ("turn", "draw", "any", "large"): {"fold": 9, "call": 3},
    ("turn", "marginal", "any", "none"): {"check": 11, "bet": 2},
    ("turn", "marginal", "any", "small"): {"call": 6, "fold": 6},
    ("turn", "marginal", "any", "large"): {"fold": 11},
    ("turn", "air", "any", "none"): {"check": 10, "bet": 1},
    ("turn", "air", "any", "small"): {"fold": 11, "call": 1},
    ("turn", "air", "any", "large"): {"fold": 14},
    ("river", "made", "any", "none"): {"bet": 14, "check": 3},
    ("river", "made", "any", "small"): {"raise": 8, "call": 9},
    ("river", "made", "any", "large"): {"call": 10, "raise": 4, "fold": 1},
    ("river", "marginal", "any", "none"): {"check": 12, "bet": 1},
    ("river", "marginal", "any", "small"): {"call": 6, "fold": 5},
    ("river", "marginal", "any", "large"): {"fold": 10, "call": 2},
    ("river", "air", "any", "none"): {"check": 11, "bet": 2},
    ("river", "air", "any", "small"): {"fold": 11, "call": 1},
    ("river", "air", "any", "large"): {"fold": 14},
}


def decide_action(game_state, player_state, legal_actions):
    # 現在状態を小さな infoset に落とし込み、そのバケットに対応する
    # regret テーブルから行動をサンプルします。
    infoset = build_infoset(game_state, player_state)
    regrets = lookup_regrets(infoset)
    strategy = regret_matching(regrets, legal_actions)
    return format_action(weighted_choice(strategy), legal_actions)


def build_infoset(game_state, player_state):
    phase = game_state["phase"]
    pressure = classify_pressure(game_state, player_state)
    position = classify_position(game_state, player_state)
    if phase == "preflop":
        bucket = classify_preflop_hand(player_state["actual_hand"])
    else:
        bucket = classify_postflop_hand(player_state["actual_hand"], game_state["community_cards"])
    return phase, bucket, position, pressure


def lookup_regrets(infoset):
    # 厳密一致からより広い既定値へフォールバックし、手で調整していない
    # 局面でも最低限は行動できるようにします。
    phase, bucket, position, pressure = infoset
    for key in (
        (phase, bucket, position, pressure),
        (phase, bucket, "any", pressure),
        (phase, bucket, position, "none"),
        (phase, bucket, "any", "none"),
    ):
        if key in REGRET_TABLE:
            return REGRET_TABLE[key]
    return {"check": 2, "call": 2, "fold": 2}


def regret_matching(regrets, legal_actions):
    legal_types = {action["type"] for action in legal_actions}
    positive = {action: max(value, 0) for action, value in regrets.items() if action in legal_types}
    total = sum(positive.values())
    if total <= 0:
        return uniform_strategy(legal_actions)
    return [(action, value / total) for action, value in positive.items()]


def uniform_strategy(legal_actions):
    weighted = []
    for action in legal_actions:
        action_type = action["type"]
        if action_type == "fold":
            weight = 1
        elif action_type in {"check", "call"}:
            weight = 3
        elif action_type in {"bet", "raise"}:
            weight = 2
        else:
            weight = 1
        weighted.append((action_type, weight))
    total = sum(weight for _, weight in weighted)
    return [(action, weight / total) for action, weight in weighted]


def weighted_choice(strategy):
    threshold = random.random()
    cumulative = 0.0
    for action, probability in strategy:
        cumulative += probability
        if threshold <= cumulative:
            return action
    return strategy[-1][0]


def format_action(action_type, legal_actions):
    for action in legal_actions:
        if action["type"] != action_type:
            continue
        payload = {"type": action_type}
        if action_type in {"bet", "raise"}:
            # 同じアクション種別でも小さめ〜中くらいのサイズを混ぜて、
            # 完全固定の動きにならないようにします。
            min_total = action["min_total"]
            max_total = action["max_total"]
            if max_total <= min_total:
                payload["amount"] = max_total
            else:
                payload["amount"] = min_total + int((max_total - min_total) * random.uniform(0.0, 0.45))
        elif "amount" in action:
            payload["amount"] = action["amount"]
        return payload
    first = legal_actions[0]
    return {"type": first["type"], "amount": first.get("amount")}


def classify_pressure(game_state, player_state):
    to_call = max(0, game_state["current_bet"] - player_state["bet_round"])
    stack = max(player_state["stack"], 1)
    ratio = to_call / stack
    if to_call == 0:
        return "none"
    if ratio <= 0.12:
        return "small"
    return "large"


def classify_position(game_state, player_state):
    seat = player_state["seat"]
    total = len(game_state["players"])
    dealer = game_state["dealer_index"]
    offset = (seat - dealer) % total
    if offset in {1, 2}:
        return "blind"
    if offset >= total - 2:
        return "late"
    return "early"


def classify_preflop_hand(hand):
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
    if suited and gap <= 1 and values[0] >= 10:
        return "strong"
    if suited and gap <= 2 and values[0] >= 7:
        return "speculative"
    if values[0] >= 11 and values[1] >= 9:
        return "medium"
    return "weak"


def classify_postflop_hand(hand, board):
    cards = hand + board
    rank = best_rank(cards)
    if rank >= 2:
        return "made"
    if rank == 1:
        board_high = max((VALUES[card[0]] for card in board), default=0)
        hole_high = max(VALUES[card[0]] for card in hand)
        return "made" if hole_high >= board_high else "marginal"
    if has_flush_draw(cards) or has_straight_draw(cards):
        return "draw"
    return "air"


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
