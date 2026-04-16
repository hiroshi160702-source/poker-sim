import random


def decide_action(game_state, player_state, legal_actions):
    # この基準 CPU は check / call を多めに選び、たまにだけ raise するので、
    # 軽い動作確認用の弱めボットとして使えます。
    #0.28の確率でレイズ、ベット
    #レイズ、ベットする場合最低金額
    raise_actions = [a for a in legal_actions if a["type"] in {"bet", "raise"}]
    if raise_actions and random.random() < 0.28:
        choice = random.choice(raise_actions) #実質一択
        return {"type": choice["type"], "amount": choice["min_total"]}
    
    #残りの0.781で重み付けした確率からランダムにアクション決定
    weighted = []
    for action in legal_actions:
        action_type = action["type"]
        if action_type == "fold":
            weighted.extend([action] * 1)
        elif action_type == "check":
            weighted.extend([action] * 4)
        elif action_type == "call":
            weighted.extend([action] * 3)
        elif action_type == "all-in":
            weighted.extend([action] * 1)

    choice = random.choice(weighted or legal_actions)
    payload = {"type": choice["type"]}
    if "amount" in choice:
        payload["amount"] = choice["amount"]
    return payload
