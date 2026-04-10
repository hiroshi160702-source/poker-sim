# Texas Hold'em Simulator

テキサスホールデムのローカル用シミュレーションゲームです。  
人間プレイヤー 1 人と CPU プレイヤーが円卓を囲む UI で、現在の手番、ベット状況、フォールド、チェック、レイズ、オールインが一目で分かるようにしています。

## 特徴

- ポーカーテーブルを囲むビジュアルレイアウト
- 人間の行動ボタンと現在ターンの強調表示
- 自分視点の勝率表示
- アクションログと直近ハンド履歴
- CPUごとに任意の Python ファイルを読み込み可能
- CPU同士のヘッズアップ対戦
- CPU同士のマルチプレイ自己対戦
- 各ハンドの結果を `logs/hand_XXXX.json` に保存

## 起動方法

```bash
cd /Users/hiroshi/UEC/lab/poker-sim
python3 -m pip install -r requirements.txt
./start_server.sh
```

ブラウザで `http://127.0.0.1:8000` を開いてください。

Mac なら [open_game.command](/Users/hiroshi/UEC/lab/poker-sim/open_game.command) をダブルクリックして起動しても大丈夫です。

停止は次です。

```bash
cd /Users/hiroshi/UEC/lab/poker-sim
./stop_server.sh
```

または [stop_game.command](/Users/hiroshi/UEC/lab/poker-sim/stop_game.command) を実行してください。

## GitHub公開と外部公開

このプロジェクトは GitHub に公開して、Render などのホスティングにそのまま載せやすい構成にしてあります。

### 1. GitHub リポジトリを作る

```bash
cd /Users/hiroshi/UEC/lab/poker-sim
git init
git add .
git commit -m "Initial poker simulator"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

`.gitignore` で `logs/` や `embedded_cpus/`、ローカルの PID / log ファイルは除外しています。

### 2. Render で公開する

リポジトリを GitHub に push したら、Render でそのリポジトリを選ぶだけで公開できます。  
このプロジェクトには [render.yaml](/Users/hiroshi/UEC/lab/poker-sim/render.yaml) と [Dockerfile](/Users/hiroshi/UEC/lab/poker-sim/Dockerfile) が入っているので、`uvicorn app.main:app --host 0.0.0.0 --port $PORT` で起動されます。

公開後は `https://...onrender.com` のようなURLで誰でもアクセスできます。

## テーブル設定

- `Starting Stack` で初期スタックを変更
- `CPU Players` でCPU参加人数を 1 人から 8 人まで変更
- `Apply Setup` で設定反映

設定反映時は卓をリセットして、新しい人数とスタックで遊べます。

## CPU 設定UI

- `Load Python File` で既存の Python ファイルを読み込み
- `Save Embedded Code` で画面内テキストエリアに貼ったコードを保存して適用

埋め込みコードは `embedded_cpus/` に保存されます。

## CPU ファイル仕様

CPU 用 Python ファイルは以下の関数を定義してください。

```python
def decide_action(game_state, player_state, legal_actions):
    return {"type": "check"}
```

- `game_state`: テーブル全体の状態
- `player_state`: 対象プレイヤーの状態
- `legal_actions`: 現在選べる行動一覧

返り値は次の形式です。

```python
{"type": "raise", "amount": 250}
```

- `type`: `fold`, `check`, `call`, `bet`, `raise`, `all-in`
- `amount`: `bet` / `raise` のときは最終ベット額

サンプル実装は `app/sample_cpus/` にあります。

- `random_agent.py`: 重み付きランダム
- `tight_agent.py`: タイト寄りのハンド選別
- `cfr_agent.py`: 情報集合を粗く抽象化し、regret matching で行動分布を選ぶ CFR-inspired CPU
- `game_theory_agent.py`: ポットオッズ、最低防衛頻度、混合戦略を使うゲーム理論寄りCPU
- `strategy_table_cpu.py`: 事前計算済みの戦略表 `infoset -> action probabilities` を読むCPU
- `table_builder_agent.py`: ポストフロップ到達を増やし、戦略表収集に向いた自己対戦用CPU

## 戦略表CPU

`strategy_table_cpu.py` は JSON の戦略表を読み込みます。標準では次を参照します。

- [example_gto.json](/Users/hiroshi/UEC/lab/poker-sim/app/sample_cpus/strategy_tables/example_gto.json)

情報集合キーは拡張版では次の形式です。

```text
phase|position|bucket|pressure|stack_bucket|texture
```

例:

```text
preflop|button|premium|small|deep|na
flop|late|draw|none|medium|two_tone
river|any|air|large|shallow|paired
```

このファイルをコピーして中身を差し替えれば、事前計算した近似戦略表を読むCPUとして使えます。

## CPU 同士の対戦

UI の `CPU vs CPU` から、2人戦の自己対戦を回せます。

- `Hero CPU Path`
- `Villain CPU Path`
- `Hands`
- `Export Strategy JSON`

を入れて `Run CPU Match` を押すと、勝敗集計を返します。  
`Export Strategy JSON` を指定すると、対戦中に観測した `infoset -> action frequency` を JSON として保存します。

UI の `CPU Multiplayer` では、複数CPUのファイルパスを1行ずつ入れて多人数卓の自己対戦を回せます。  
結果には各CPUの `wins`、`profit`、`1位率`、`平均獲得チップ`、席順ごとの成績、最近のハンド結果、フェーズ別の訪問数が含まれます。

CLI で戦略表を生成する場合は次です。

```bash
cd /Users/hiroshi/UEC/lab/poker-sim
python3 tools/build_strategy_table.py \
  --hero app/sample_cpus/cfr_agent.py \
  --villain app/sample_cpus/tight_agent.py \
  --hands 500 \
  --out app/sample_cpus/strategy_tables/generated_from_selfplay.json
```
