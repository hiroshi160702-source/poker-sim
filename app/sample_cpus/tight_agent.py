RANKS = "23456789TJQKA"
VALUES = {rank: index for index, rank in enumerate(RANKS, start=2)}


def decide_action(game_state, player_state, legal_actions):
    # 分かりやすくタイトな戦略です。強い手だけ続行し、微妙な手では
    # 無理に攻めず受け身に寄せます。
    card_a, card_b = player_state["actual_hand"]
    suited = card_a[1] == card_b[1]
    high = sorted([VALUES[card_a[0]], VALUES[card_b[0]]], reverse=True)
    pair = high[0] == high[1]
    premium = pair and high[0] >= 10
    broadway = high[0] >= 13 and high[1] >= 11
    playable = premium or broadway or (suited and high[0] >= 11 and high[1] >= 10)

    if playable:
        for action in legal_actions:
            if action["type"] in {"raise", "bet"}:
                return {"type": action["type"], "amount": action["min_total"]}
        for preferred in ("call", "check", "all-in"):
            for action in legal_actions:
                if action["type"] == preferred:
                    return {"type": preferred, "amount": action.get("amount")}

    for preferred in ("check", "fold", "call"):
        for action in legal_actions:
            if action["type"] == preferred:
                payload = {"type": preferred}
                if "amount" in action:
                    payload["amount"] = action["amount"]
                return payload

    fallback = legal_actions[0]
    return {"type": fallback["type"], "amount": fallback.get("amount")}
