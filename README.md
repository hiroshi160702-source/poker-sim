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
- [pluribus_agent.py](app/sample_cpus/pluribus_agent.py)
- [game_theory_agent.py](app/sample_cpus/game_theory_agent.py)
- [strategy_table_cpu.py](app/sample_cpus/strategy_table_cpu.py)
- [table_builder_agent.py](app/sample_cpus/table_builder_agent.py)

`pluribus_agent.py` は Pluribus 風の軽量近似 CPU です。事前戦略に近いヒューリスティックを持ち、
各局面で fold/check/call と `1/3 pot`, `1/2 pot`, `1 pot`, `all-in` の抽象サイズを評価して選びます。

## Self-Play

画面の `CPU Multiplayer` から複数 CPU の `.py` ファイルを同時に対戦させられます。`Add CPU Slot` を使うと、同じ戦略ファイルも複数席へ入れられます。  
結果には次が含まれます。

- 勝利数
- 総獲得チップ
- 1 位回数と 1 位率
- 1 ハンドあたり平均獲得チップ
- 席順ごとの成績
- 戦略表ダウンロード

## Strategy Tables

`strategy_table_cpu.py` は事前生成した戦略表 JSON を読み込みます。  
情報集合キーは次の形式です。

```text
phase|player_count|position|bucket|pressure|stack_bucket|texture
```

例:

```text
preflop|2p|button|premium|small|deep|na
flop|3p|late|draw|none|medium|two_tone
river|any|any|air|large|shallow|paired
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
直下には本番寄りの表を置き、`smoke/`, `visits/`, `archive/` に補助出力や試作を分けています。

Monte Carlo CFR 近似でヘッズアップ用の戦略表を作るには次も使えます。

```bash
python3 tools/strategy.py \
  --iterations 5000 \
  --out app/sample_cpus/strategy_tables/cfr_generated.json
```

6-max などのマルチプレイヤー向けに、全ストリート対応の近似 MCCFR で戦略表を作るには次も使えます。

```bash
python3 tools/strategy_preflop_multiway.py \
  --players 6 \
  --iterations 5000 \
  --min-visits 25 \
  --out app/sample_cpus/strategy_tables/multiway_6max_mccfr.json
```

以前と同じプリフロップ専用の出力にしたい場合は `--preflop-only` を付けてください。
長時間学習を途中から再開したい場合は、同時に出力される checkpoint JSON を `--resume-state` に渡します。
現在の学習器はルートから1本のtrajectoryをサンプリングして終局まで進め、そのtrajectory上に現れた全プレイヤーの infoset を後からまとめて regret 更新します。
各infosetでは合法な各アクションについて「もし別行動をしていたら」をロールアウトで評価し、`utility(action) - node_utility` を regret に加えます。
これにより、同じtrajectory上でブラフした側・ブラフに直面した側の両方が学習対象になります。
平均戦略は Linear CFR として、古いiterationより新しいiterationを大きい重みで `strategy_sums` に加えます。
また、学習が1000 iterationを超えた後は、非常に大きな負regretを持つtraverser行動を95%のiterationで評価対象から外します。ただし5%のiterationでは全行動を探索します。
初回訪問の infoset では、まず `fold/call/raise` などの基本アクショングループを等確率にし、`raise` や `bet` が選ばれた場合だけ `small/medium/large` のサイズを等確率に割り振ります。
`all-in` はルール上の合法アクションとしては残しますが、blueprint学習の選択肢には `bet_large` / `raise_large` がスタック上限に潰れる局面だけ含めます。
以前入れていた `air + jam` などの学習時安全制約は `docs/removed_learning_safety_constraints.md` に記録し、現在のblueprint学習では無効化しています。

```bash
python3 tools/strategy_preflop_multiway.py \
  --players 6 \
  --iterations 5000 \
  --resume-state app/sample_cpus/strategy_tables/multiway_6max_mccfr_state.json \
  --out app/sample_cpus/strategy_tables/multiway_6max_mccfr.json
```

`pluribus_agent.py` はデフォルトで `app/sample_cpus/strategy_tables/pluribus_blueprint_6p_*.json` から、checkpoint の `completed_iterations` が最大の JSON を blueprint として自動選択します。
`PLURIBUS_BLUEPRINT_PATH` は通常この自動選択より優先されません。特定ファイルに固定したい場合だけ `PLURIBUS_BLUEPRINT_PINNED=1` と一緒に指定してください。
blueprint 参照時は、まず完全一致の infoset、次に `any` へ丸めた既存キーを探します。
それでも未学習なら、stack bucket、player count、board texture などの一部軸を自由にして近い実在 infoset を集め、
各行の戦略を平均した補間戦略を使います。補間もできない場合だけヒューリスティック fallback に落ちます。
局面ごとに相手レンジを簡易更新し、root/subgame CFR 近似を `PLURIBUS_SUBGAME_SECONDS` 秒だけ走らせます。この値は最大 15 秒に制限されます。
意思決定の理由を追いたい場合は `pluribus_whitebox_agent.py` を使います。通常の Pluribus と同じ意思決定を行い、
`PLURIBUS_TRACE_PATH` の JSONL に infoset、候補行動、blueprint確率、最終確率、選択理由を書き出します。

```bash
PLURIBUS_BLUEPRINT_PATH=app/sample_cpus/strategy_tables/pluribus_blueprint_6p_500000.json \
PLURIBUS_TRACE_PATH=logs/pluribus_whitebox_decisions.jsonl \
python3 - <<'PY'
from pathlib import Path
from app.selfplay import run_multiway_cpu_match

agent = str(Path("app/sample_cpus/pluribus_whitebox_agent.py").resolve())
run_multiway_cpu_match(
    logs_dir=Path("logs"),
    embedded_cpu_dir=Path("embedded_cpus"),
    cpu_paths=[agent, agent, agent],
    hands=10,
    starting_stack=5000,
)
PY
```

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
