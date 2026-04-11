# Texas Hold'em Simulator

テキサスホールデムをブラウザで遊べるシミュレーションゲームです。  
人間 vs CPU だけでなく、CPU 同士のマルチプレイ自己対戦や戦略表の生成にも対応しています。

Live Demo:

- https://poker-sim.onrender.com/

Repository:

- https://github.com/hiroshi160702-source/poker-sim

## Features

- テーブルを囲むポーカーUI
- 手番、ベット、チェック、フォールド、レイズ、オールインが見やすい表示
- 自分視点の勝率表示
- アクションログとハンド履歴
- CPU を Python ファイルで差し替え可能
- `.py` ファイルを画面からアップロードして読み込み可能
- CPU コードを画面から保存して読み込み可能
- マルチプレイ自己対戦と戦略表の書き出し

## Quick Start

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload
```

起動後、ブラウザで `http://127.0.0.1:8000` を開いてください。

Mac では次も使えます。

```bash
./start_server.sh
```

停止は次です。

```bash
./stop_server.sh
```

## How To Play

1. `Start / Next Hand` でゲーム開始
2. 自分の番で `Fold / Check / Call / Bet / Raise / All-in` を選択
3. `Bet / Raise To` に金額を入力してベットサイズを指定
4. `Table Setup` で初期スタックと CPU 人数を変更
5. `CPU Files` から各 CPU の `.py` ファイルをアップロードして差し替え

## CPU Bots

CPU は Python ファイルで定義します。必要なのは `decide_action` だけです。

```python
def decide_action(game_state, player_state, legal_actions):
    return {"type": "check"}
```

- `game_state`: テーブル全体の状態
- `player_state`: 対象プレイヤーの状態
- `legal_actions`: その場で選べる合法手

返り値の例:

```python
{"type": "raise", "amount": 250}
```

利用可能な `type`:

- `fold`
- `check`
- `call`
- `bet`
- `raise`
- `all-in`

サンプルは [app/sample_cpus](app/sample_cpus) にあります。

- [random_agent.py](app/sample_cpus/random_agent.py)
- [tight_agent.py](app/sample_cpus/tight_agent.py)
- [cfr_agent.py](app/sample_cpus/cfr_agent.py)
- [game_theory_agent.py](app/sample_cpus/game_theory_agent.py)
- [strategy_table_cpu.py](app/sample_cpus/strategy_table_cpu.py)
- [table_builder_agent.py](app/sample_cpus/table_builder_agent.py)

## Self-Play

画面の `CPU Multiplayer` から複数 CPU の `.py` ファイルを同時に対戦させられます。  
結果には次が含まれます。

- 勝利数
- 総獲得チップ
- 1 位回数と 1 位率
- 1 ハンドあたり平均獲得チップ
- 席順ごとの成績

## Strategy Tables

`strategy_table_cpu.py` は事前生成した戦略表 JSON を読み込みます。  
情報集合キーは次の形式です。

```text
phase|position|bucket|pressure|stack_bucket|texture
```

例:

```text
preflop|button|premium|small|deep|na
flop|late|draw|none|medium|two_tone
river|any|air|large|shallow|paired
```

戦略表の生成例:

```bash
python3 tools/build_strategy_table.py \
  --hero app/sample_cpus/cfr_agent.py \
  --villain app/sample_cpus/tight_agent.py \
  --hands 500 \
  --out app/sample_cpus/strategy_tables/generated_from_selfplay.json
```

生成済みのサンプル表は [app/sample_cpus/strategy_tables](app/sample_cpus/strategy_tables) にあります。

## Project Structure

```text
app/
  main.py              FastAPI entrypoint
  engine.py            Hold'em game engine
  selfplay.py          CPU self-play runners
  static/              Frontend files
  sample_cpus/         Sample CPU bots
  strategy_tables/     Infoset helpers
tools/
  build_strategy_table.py
```

## Deploy

このリポジトリは Docker / Render で公開しやすい構成です。

- [Dockerfile](Dockerfile)
- [render.yaml](render.yaml)

Render に GitHub リポジトリを接続すれば、そのまま Web サービスとして公開できます。

## Notes

- ログは `logs/` に保存されます
- 画面から保存した CPU コードは `embedded_cpus/` に保存されます
- 生成された戦略表は近似自己対戦ベースであり、厳密な GTO 解ではありません
