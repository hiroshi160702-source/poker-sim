// ポーカーテーブル UI と CPU 自己対戦ダッシュボード全体を制御します。
const stateUrl = "/api/state";
const newHandUrl = "/api/new-hand";
const actionUrl = "/api/action";
const uploadCpuFileUrl = "/api/upload-cpu-file";
const resetTableUrl = "/api/reset-table";
const configureTableUrl = "/api/configure-table";
const cpuMultiMatchUrl = "/api/run-cpu-multiplayer";
const cpuMultiStartUrl = "/api/start-cpu-multiplayer";
const cpuMultiJobBaseUrl = "/api/cpu-multiplayer-jobs";

const seatIds = [0, 1, 2, 3, 4, 5, 6, 7, 8];
let currentState = null;
let revealFoldedHands = false;
let requestInFlight = false;
let lastRaiseBounds = null;
const defaultCpuCode = "";
const uploadStatusBySeat = {};
let cpuMultiSelectionStatus = "No files selected.";
let cpuMultiSelectionTone = "muted";
let cpuMultiSlots = [
  { id: 1, file: null, strategyFile: null, count: 1, label: "No file selected." },
  { id: 2, file: null, strategyFile: null, count: 1, label: "No file selected." },
];
let freezeCpuPanels = false;
let cpuConfigSignature = null;
let cpuMultiSlotsSignature = null;
let lastStrategyTable = null;
let lastStrategyFilename = "strategy_table.json";
let cpuMultiJobId = null;
let cpuMultiJobPollHandle = null;

function setUploadStatus(elementId, message, tone = "muted") {
  const node = document.getElementById(elementId);
  if (!node) return;
  node.className = `upload-status ${tone}`;
  node.textContent = message;
}

function hasPendingFileSelection() {
  const perSeatFiles = Array.from(document.querySelectorAll('input[id^="cpu-file-"]'))
    .some((input) => input.files && input.files.length > 0);
  const perSeatJsonFiles = Array.from(document.querySelectorAll('input[id^="cpu-json-"]'))
    .some((input) => input.files && input.files.length > 0);
  return perSeatFiles
    || perSeatJsonFiles
    || cpuMultiSlots.some((slot) => Boolean(slot.file) || Boolean(slot.strategyFile));
}

function setFreezeCpuPanels(value) {
  freezeCpuPanels = value;
}

async function apiFetch(url, options = {}) {
  // UI からの主要な API 呼び出しはここを通し、リクエスト競合とエラー表示を
  // できるだけ一貫させます。
  requestInFlight = true;
  try {
    const response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: "Request failed" }));
      throw new Error(payload.detail || "Request failed");
    }
    return await response.json();
  } finally {
    requestInFlight = false;
  }
}

async function uploadCpuFile(file, seat = null) {
  let strategyFile = null;
  if (typeof seat === "object" && seat !== null) {
    strategyFile = seat.strategyFile || null;
    seat = seat.seat ?? null;
  }
  const formData = new FormData();
  formData.append("file", file);
  if (strategyFile) {
    formData.append("strategy_file", strategyFile);
  }
  if (seat !== null && seat !== undefined) {
    formData.append("seat", String(seat));
  }

  requestInFlight = true;
  try {
    const response = await fetch(uploadCpuFileUrl, {
      method: "POST",
      body: formData,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: "Upload failed" }));
      throw new Error(payload.detail || "Upload failed");
    }
    return await response.json();
  } finally {
    requestInFlight = false;
  }
}

function stateUrlWithReveal() {
  const url = new URL(stateUrl, window.location.origin);
  if (revealFoldedHands) {
    url.searchParams.set("reveal_folded", "true");
  }
  return `${url.pathname}${url.search}`;
}

function suitClass(card) {
  return card.includes("H") || card.includes("D") ? "red" : "";
}

function suitSymbol(suit) {
  if (suit === "S") return "♠";
  if (suit === "H") return "♥";
  if (suit === "D") return "♦";
  if (suit === "C") return "♣";
  return "";
}

function rankLabel(rank) {
  if (rank === "T") return "10";
  return rank;
}

function renderCards(cards) {
  if (!cards || cards.length === 0) {
    return `<span class="seat-stack">No cards</span>`;
  }

  return cards
    .map((card) => {
      if (card === "??") {
        return `
          <span class="card back">
            <span class="card-back-pattern"></span>
          </span>
        `;
      }

      const rank = rankLabel(card[0]);
      const suit = suitSymbol(card[1]);
      const cls = suitClass(card);
      return `
        <span class="card ${cls}">
          <span class="card-corner top">
            <span class="card-rank">${rank}</span>
            <span class="card-suit">${suit}</span>
          </span>
          <span class="card-center-suit">${suit}</span>
          <span class="card-corner bottom">
            <span class="card-rank">${rank}</span>
            <span class="card-suit">${suit}</span>
          </span>
        </span>
      `;
    })
    .join("");
}

function actionStatusClass(action) {
  const normalized = action.toLowerCase();
  if (normalized.includes("small blind")) return "blind";
  if (normalized.includes("big blind")) return "blind";
  if (normalized.includes("fold")) return "fold";
  if (normalized.includes("check")) return "check";
  if (normalized.includes("call")) return "call";
  if (normalized.includes("raise")) return "raise";
  if (normalized.includes("bet")) return "bet";
  if (normalized.includes("all-in")) return "all-in";
  if (normalized.includes("waiting")) return "waiting";
  return "neutral";
}

function downloadStrategyTable() {
  if (!lastStrategyTable) return;
  const blob = new Blob([JSON.stringify(lastStrategyTable, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = lastStrategyFilename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function setCpuMultiRunning(isRunning) {
  document.getElementById("run-cpu-multi-btn").disabled = isRunning;
  document.getElementById("add-cpu-slot-btn").disabled = isRunning;
  document.getElementById("remove-cpu-slot-btn").disabled = isRunning;
}

function stopCpuMultiPolling() {
  if (cpuMultiJobPollHandle) {
    clearTimeout(cpuMultiJobPollHandle);
    cpuMultiJobPollHandle = null;
  }
}

function renderCpuMatchProgress(job = null) {
  const label = document.getElementById("cpu-multi-progress-label");
  const bar = document.getElementById("cpu-multi-progress-bar");
  const status = document.getElementById("cpu-multi-progress-status");
  const preview = document.getElementById("cpu-multi-progress-preview");

  if (!job) {
    label.textContent = "Idle";
    bar.style.width = "0%";
    status.textContent = "まだ自己対戦は実行していません。";
    preview.innerHTML = "";
    return;
  }

  label.textContent = job.status || "Running";
  bar.style.width = `${Math.max(0, Math.min(100, job.percent || 0))}%`;
  const elapsed = formatDuration(job.elapsed_seconds);
  const remaining = formatDuration(job.estimated_remaining_seconds);
  const replayNote = job.capture_replay === false ? " Live replay disabled for large runs." : "";
  status.textContent = `${job.message || ""} ${job.completed_hands || 0} / ${job.total_hands || 0} hands | Elapsed ${elapsed}${remaining ? ` | ETA ${remaining}` : ""}.${replayNote}`;
  preview.innerHTML = (job.leaderboard_preview || [])
    .map(
      (player) => `
        <div class="list-card">
          <strong>${player.name}</strong>
          <div>Profit ${player.profit}</div>
          <div>Wins ${player.wins}</div>
          <div>Avg / hand ${player.avg_profit}</div>
        </div>
      `
    )
    .join("");
}

function renderCpuReplaySnapshot(snapshot) {
  // ライブ再生は最新スナップショットだけを表示し、長い自己対戦でも
  // ブラウザ側が重くなりすぎないようにします。
  const container = document.getElementById("cpu-multi-live-replay");
  if (!snapshot) {
    container.innerHTML = `<div class="panel-note">ライブ再生はここに表示されます。</div>`;
    return;
  }

  const winners = (snapshot.last_winners || []).map((winner) => `${winner.name} +${winner.amount}`).join(" / ");
  container.innerHTML = `
    <div class="replay-card">
      <div class="replay-header">
        <strong>Live Replay: Hand #${snapshot.hand_id}</strong>
        <span class="pill">${(snapshot.phase || "waiting").toUpperCase()}</span>
      </div>
      <div class="list-card">
        <div>${snapshot.table_message}</div>
        <div>Pot ${snapshot.pot}</div>
        <div>Board</div>
        <div class="cards-row">${renderCards(snapshot.community_cards || [])}</div>
        ${winners ? `<div>Winners: ${winners}</div>` : ""}
      </div>
      <div class="replay-players">
        ${(snapshot.players || [])
          .map(
            (player) => `
              <div class="replay-player ${!player.in_hand ? "out" : ""}">
                <div class="replay-player-main">
                  <strong>${player.name}</strong>
                  <div class="cards-row">${renderCards(player.hand || [])}</div>
                </div>
                <div class="replay-player-meta">
                  <div>${player.last_action}</div>
                  <div>Stack ${player.stack}</div>
                  ${player.win_amount ? `<div>Won +${player.win_amount}</div>` : ""}
                </div>
              </div>
            `
          )
          .join("")}
      </div>
    </div>
  `;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return "";
  }
  const totalSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const secs = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${secs}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${secs}s`;
  }
  return `${secs}s`;
}

function renderSeats(players) {
  // 座席は楕円上に動的配置し、少人数卓から 9 人卓まで同じ描画処理で扱います。
  const totalPlayers = players.length;
  seatIds.forEach((seatId) => {
    const node = document.getElementById(`seat-${seatId}`);
    const player = players.find((entry) => entry.seat === seatId);
    if (!player) {
      node.className = `seat hidden seat-${seatId}`;
      node.innerHTML = "";
      node.style.left = "";
      node.style.top = "";
      node.style.transform = "";
      return;
    }
    const roleBadges = [
      player.is_dealer ? "D" : "",
      player.is_small_blind ? "SB" : "",
      player.is_big_blind ? "BB" : "",
    ].filter(Boolean).join(" ");
    const angleStep = 360 / totalPlayers;
    const angle = (90 + player.seat * angleStep) * (Math.PI / 180);
    const centerX = 50;
    const centerY = 51;
    const radiusX = totalPlayers >= 8 ? 39 : 35;
    const radiusY = totalPlayers >= 8 ? 39 : 34;
    const x = centerX + Math.cos(angle) * radiusX;
    const y = centerY - Math.sin(angle) * radiusY;
    node.className = `seat ${player.is_current_turn ? "current-turn" : ""} ${player.folded ? "folded" : ""} ${!player.in_hand ? "out" : ""} seat-${player.seat}`;
    node.style.left = `${x}%`;
    node.style.top = `${y}%`;
    node.style.transform = "translate(-50%, -50%)";
    node.innerHTML = `
      <div class="seat-header">
        <div>
          <div class="seat-name">${player.name}</div>
          <div class="seat-stack">${player.stack} chips</div>
        </div>
        <span class="seat-status ${actionStatusClass(player.last_action)}">${player.last_action}</span>
      </div>
      <div class="cards-block">${renderCards(player.hand)}</div>
      <div class="seat-footer">
        <span class="seat-bet">Round Bet ${player.bet_round}</span>
        <span class="seat-odds">${roleBadges || "Seat"}</span>
      </div>
      <div class="seat-footer">
        <span class="seat-bet">${player.all_in ? "All-in" : player.in_hand ? "Active" : "Out"}</span>
        <strong>${player.hand_label || (player.won_last ? `+${player.win_amount}` : "")}</strong>
      </div>
      ${player.cpu_error ? `<div class="panel-note">${player.cpu_error}</div>` : ""}
    `;
  });
}

function renderCommunity(cards) {
  document.getElementById("community-cards").innerHTML = renderCards(cards);
}

function renderActionButtons(state) {
  // 合法アクションの判定はサーバー側で行い、ブラウザ側は表示と
  // 金額の範囲補正だけを担当します。
  const container = document.getElementById("action-buttons");
  const indicator = document.getElementById("turn-indicator");
  const amountInput = document.getElementById("bet-amount");
  const human = state.players.find((player) => player.is_human);
  const isHumanTurn = human && human.is_current_turn;

  if (!human) return;

  const legalActions = human.legal_actions || [];
  if (!isHumanTurn || legalActions.length === 0) {
    indicator.textContent = state.awaiting_new_hand
      ? "Hand is complete. Start the next hand when ready."
      : state.current_turn === null
        ? "Waiting..."
        : `Current turn: ${state.players[state.current_turn].name}`;
    container.innerHTML = "";
    amountInput.disabled = true;
    lastRaiseBounds = null;
    return;
  }

  indicator.textContent = "Your turn. Choose fold, check, bet, raise, or all-in.";
  amountInput.disabled = false;

  const raiseLike = legalActions.find((action) => action.type === "raise" || action.type === "bet");
  if (raiseLike) {
    amountInput.min = raiseLike.min_total;
    amountInput.max = raiseLike.max_total;
    const currentValue = Number(amountInput.value);
    const nextValue = Number.isFinite(currentValue) && currentValue >= raiseLike.min_total && currentValue <= raiseLike.max_total
      ? currentValue
      : raiseLike.min_total;
    amountInput.value = nextValue;
    lastRaiseBounds = { min: raiseLike.min_total, max: raiseLike.max_total };
  } else {
    lastRaiseBounds = null;
  }

  container.innerHTML = "";
  legalActions.forEach((action) => {
    const button = document.createElement("button");
    button.className = `action-btn ${action.type}`;
    button.textContent = action.label;
    button.onclick = async () => {
      if (requestInFlight) return;
      try {
        indicator.textContent = "Sending action...";
        const payload = { action: action.type };
        if (action.type === "bet" || action.type === "raise") {
          let amount = Number(amountInput.value);
          if (!Number.isFinite(amount)) {
            amount = action.min_total;
          }
          amount = Math.max(action.min_total, Math.min(action.max_total, amount));
          payload.amount = amount;
          amountInput.value = amount;
        }
        const nextState = await apiFetch(actionUrl, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        renderState(nextState);
      } catch (error) {
        indicator.textContent = "Action failed. Check amount and try again.";
        alert(error.message);
      }
    };
    container.appendChild(button);
  });
}

function renderLogs(logs) {
  const toCard = (line) => {
    const parts = line.split(" | ");
    if (parts.length >= 3) {
      const [street, actor, ...rest] = parts;
      return `
        <div class="list-card log-card">
          <div class="log-card-header">
            <span class="pill">${street}</span>
            <strong>${actor}</strong>
          </div>
          <div>${rest.join(" | ")}</div>
        </div>
      `;
    }
    return `<div class="list-card log-card">${line}</div>`;
  };

  document.getElementById("log-list").innerHTML = logs
    .slice()
    .reverse()
    .map(toCard)
    .join("");
}

function renderHistory(history) {
  document.getElementById("history-list").innerHTML = history
    .map(
      (item) => `
        <div class="list-card">
          <strong>Hand #${item.hand_id}</strong>
          <div>${item.result}</div>
          <div>Board: ${(item.community || []).join(" ") || "-"}</div>
          ${item.details ? `<div>${item.details}</div>` : ""}
        </div>
      `
    )
    .join("");
}

function renderHeroWinRate(state) {
  const hero = state.players.find((player) => player.is_human);
  document.getElementById("hero-win-rate-card").innerHTML = `
    <div class="hero-win-rate-value">${state.hero_win_rate}%</div>
    <div class="hero-win-rate-note">
      ${hero ? `${hero.name} 視点の現在勝率です。CPUごとの勝率は表示していません。` : "Human player not found."}
    </div>
    <div class="list-card">${state.phase.toUpperCase()} / Pot ${state.pot}</div>
  `;
}

function renderCpuMatchResult(result) {
  if (!result) {
    document.getElementById("cpu-selfplay-summary").textContent = "結果はここに表示されます。";
    document.getElementById("download-strategy-btn").disabled = true;
    lastStrategyTable = null;
    document.getElementById("cpu-match-result").innerHTML = `<div class="list-card">No self-play run yet.</div>`;
    renderCpuReplaySnapshot(null);
    return;
  }
  document.getElementById("cpu-selfplay-summary").textContent =
    `${result.player_count} 人で ${result.hands} ハンド自動対戦しました。順位表と最近の結果を下に表示しています。`;
  lastStrategyTable = result.strategy_table || null;
  lastStrategyFilename = result.strategy_table_filename || "strategy_table.json";
  document.getElementById("download-strategy-btn").disabled = !lastStrategyTable;
  renderCpuReplaySnapshot(result.last_replay_snapshot || null);
  renderCpuMatchProgress({
    status: "completed",
    completed_hands: result.hands,
    total_hands: result.hands,
    percent: 100,
    message: "CPU self-play finished.",
    leaderboard_preview: result.leaderboard || [],
  });
  const leaderboard = (result.leaderboard || [])
    .map(
      (player) => `
        <div class="list-card">
          <strong>${player.name}</strong>
          <div>Wins ${player.wins}</div>
          <div>Profit ${player.profit}</div>
          <div>1st ${player.first_places} (${player.first_place_rate}%)</div>
          <div>Avg / hand ${player.avg_profit}</div>
          <div>${player.cpu_path}</div>
        </div>
      `
    )
    .join("");
  const seatStats = (result.seat_stats || [])
    .map(
      (seat) => `
        <div class="list-card">
          <strong>Seat ${seat.seat}: ${seat.name}</strong>
          <div>Wins ${seat.wins}</div>
          <div>1st ${seat.first_places} (${seat.first_place_rate}%)</div>
          <div>Avg / hand ${seat.avg_profit}</div>
          <div>Total profit ${seat.profit}</div>
        </div>
      `
    )
    .join("");
  const recent = (result.recent_results || [])
    .map(
      (item) => `
        <div class="list-card">
          <strong>Hand #${item.hand_id}</strong>
          <div>${(item.players || []).map((player) => `${player.name} ${player.delta}`).join(" / ")}</div>
          <div>${item.message}</div>
        </div>
      `
    )
    .join("");
  document.getElementById("cpu-match-result").innerHTML = `
      <div class="list-card">
        <strong>${result.player_count} Players Multiplayer</strong>
        <div>Hands ${result.hands}</div>
        <div>Visited infosets ${result.visited_infosets}</div>
        <div>Phases ${Object.entries(result.phase_breakdown || {}).map(([phase, count]) => `${phase} ${count}`).join(" / ")}</div>
        ${result.exported_strategy_path ? `<div>Exported: ${result.exported_strategy_path}</div>` : ""}
        ${lastStrategyTable ? `<div>Strategy table is ready to download.</div>` : ""}
      </div>
    ${leaderboard}
    ${seatStats}
    ${recent || `<div class="list-card">No recent result.</div>`}
  `;
}

function renderCpuConfig(players) {
  // こちらはライブ卓に対する席ごとの CPU 差し替え用アップロードです。
  const cpuPlayers = players.filter((player) => !player.is_human);
  document.getElementById("cpu-config-list").innerHTML = cpuPlayers
    .map(
      (player) => `
        <div class="cpu-config">
          <strong>${player.name}</strong>
          <div class="cpu-upload-fields">
            <label class="upload-field-label" for="cpu-file-${player.seat}">CPU Script (.py)</label>
            <input id="cpu-file-${player.seat}" type="file" accept=".py" />
            <label class="upload-field-label" for="cpu-json-${player.seat}">Strategy JSON (.json, optional)</label>
            <input id="cpu-json-${player.seat}" type="file" accept=".json,application/json" />
          </div>
          <div id="cpu-upload-status-${player.seat}" class="upload-status muted">${uploadStatusBySeat[player.seat] || "No file selected."}</div>
          <div class="panel-note">Current: ${player.cpu_path || "No CPU loaded."}</div>
        </div>
      `
    )
    .join("");

  cpuPlayers.forEach((player) => {
    const fileInput = document.getElementById(`cpu-file-${player.seat}`);
    const jsonInput = document.getElementById(`cpu-json-${player.seat}`);

    const uploadSeatBundle = async () => {
      const file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
      const strategyFile = jsonInput.files && jsonInput.files[0] ? jsonInput.files[0] : null;
      setFreezeCpuPanels(Boolean(file) || Boolean(strategyFile));

      if (!file) {
        uploadStatusBySeat[player.seat] = strategyFile
          ? `Strategy JSON selected: ${strategyFile.name}. Select a .py file to upload.`
          : "No file selected.";
        setUploadStatus(`cpu-upload-status-${player.seat}`, uploadStatusBySeat[player.seat], "muted");
        return;
      }

      try {
        const detail = strategyFile ? `${file.name} + ${strategyFile.name}` : file.name;
        uploadStatusBySeat[player.seat] = `Uploading ${detail}...`;
        setUploadStatus(`cpu-upload-status-${player.seat}`, `Uploading ${detail}...`, "muted");
        const nextState = await uploadCpuFile(file, { seat: player.seat, strategyFile });
        setFreezeCpuPanels(false);
        uploadStatusBySeat[player.seat] = strategyFile
          ? `Uploaded: ${file.name} with ${strategyFile.name}`
          : `Uploaded: ${file.name}`;
        renderState(nextState);
        setUploadStatus(`cpu-upload-status-${player.seat}`, uploadStatusBySeat[player.seat], "success");
      } catch (error) {
        setFreezeCpuPanels(false);
        uploadStatusBySeat[player.seat] = `Upload failed: ${error.message}`;
        setUploadStatus(`cpu-upload-status-${player.seat}`, `Upload failed: ${error.message}`, "error");
        alert(error.message);
      }
    };

    fileInput.addEventListener("change", uploadSeatBundle);
    jsonInput.addEventListener("change", uploadSeatBundle);
  });
}

function ensureCpuConfigRendered(players) {
  const cpuPlayers = players.filter((player) => !player.is_human);
  const nextSignature = cpuPlayers.map((player) => `${player.seat}:${player.name}`).join("|");
  if (cpuConfigSignature === nextSignature && document.getElementById("cpu-config-list").children.length > 0) {
    return;
  }
  cpuConfigSignature = nextSignature;
  renderCpuConfig(players);
}

function renderCpuMultiSlots() {
  // 自己対戦では戦略スロット方式を使い、1 つの Python ファイルを
  // 複数席へまとめて割り当てられるようにしています。
  const container = document.getElementById("cpu-multi-slots");
  container.innerHTML = cpuMultiSlots
    .map(
      (slot, index) => `
        <div class="cpu-config">
          <strong>CPU Slot ${index + 1}</strong>
          <div class="cpu-config-row">
            <div class="cpu-upload-fields">
              <label class="upload-field-label" for="cpu-multi-file-${slot.id}">CPU Script (.py)</label>
              <input id="cpu-multi-file-${slot.id}" type="file" accept=".py" />
              <label class="upload-field-label" for="cpu-multi-json-${slot.id}">Strategy JSON (.json, optional)</label>
              <input id="cpu-multi-json-${slot.id}" type="file" accept=".json,application/json" />
            </div>
            <div class="amount-controls">
              <label for="cpu-multi-count-${slot.id}">Players</label>
              <input id="cpu-multi-count-${slot.id}" type="number" min="1" max="9" step="1" value="${slot.count}" />
            </div>
          </div>
          <div id="cpu-multi-slot-status-${slot.id}" class="upload-status muted">${slot.label}</div>
        </div>
      `
    )
    .join("");

  cpuMultiSlots.forEach((slot) => {
    document.getElementById(`cpu-multi-file-${slot.id}`).addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      const jsonInput = document.getElementById(`cpu-multi-json-${slot.id}`);
      const strategyFile = jsonInput?.files && jsonInput.files[0] ? jsonInput.files[0] : null;
      setFreezeCpuPanels(Boolean(file) || cpuMultiSlots.some((entry) => entry.file));
      slot.file = file || null;
      slot.strategyFile = strategyFile;
      slot.label = file
        ? `Selected: ${file.name}${strategyFile ? ` + ${strategyFile.name}` : ""}`
        : "No file selected.";
      setUploadStatus(`cpu-multi-slot-status-${slot.id}`, slot.label, "muted");
      updateCpuMultiSelectionSummary();
    });
    document.getElementById(`cpu-multi-json-${slot.id}`).addEventListener("change", (event) => {
      const strategyFile = event.target.files && event.target.files[0];
      slot.strategyFile = strategyFile || null;
      slot.label = slot.file
        ? `Selected: ${slot.file.name}${slot.strategyFile ? ` + ${slot.strategyFile.name}` : ""}`
        : (slot.strategyFile ? `JSON ready: ${slot.strategyFile.name}` : "No file selected.");
      setUploadStatus(`cpu-multi-slot-status-${slot.id}`, slot.label, "muted");
      updateCpuMultiSelectionSummary();
    });
    document.getElementById(`cpu-multi-count-${slot.id}`).addEventListener("input", (event) => {
      const nextValue = Number(event.target.value);
      slot.count = Math.max(1, Math.min(9, Number.isFinite(nextValue) ? Math.floor(nextValue) : 1));
      event.target.value = slot.count;
      updateCpuMultiSelectionSummary();
    });
  });
}

function ensureCpuMultiSlotsRendered() {
  const nextSignature = cpuMultiSlots.map((slot) => slot.id).join("|");
  if (cpuMultiSlotsSignature === nextSignature && document.getElementById("cpu-multi-slots").children.length > 0) {
    return;
  }
  cpuMultiSlotsSignature = nextSignature;
  renderCpuMultiSlots();
}

function updateCpuMultiSelectionSummary() {
  const selected = cpuMultiSlots.filter((slot) => slot.file);
  const totalPlayers = selected.reduce((sum, slot) => sum + slot.count, 0);
  if (selected.length === 0) {
    cpuMultiSelectionStatus = "No files selected.";
    cpuMultiSelectionTone = "muted";
  } else {
    cpuMultiSelectionStatus = `Selected ${selected.length} strategies / ${totalPlayers} players: ${selected.map((slot) => `${slot.file.name}${slot.strategyFile ? ` + ${slot.strategyFile.name}` : ""} x${slot.count}`).join(", ")}`;
    cpuMultiSelectionTone = "muted";
  }
  setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
}

function renderState(state) {
  currentState = state;
  document.getElementById("table-message").textContent = state.table_message;
  document.getElementById("phase-pill").textContent = state.phase.toUpperCase();
  document.getElementById("pot-pill").textContent = `Pot ${state.pot}`;
  document.getElementById("pot-value").textContent = state.pot;
  document.getElementById("reveal-folded-btn").textContent =
    revealFoldedHands ? "Hide Folded Hands" : "Reveal Folded Hands";
  document.getElementById("reveal-folded-btn").disabled = state.phase !== "showdown";
  document.getElementById("starting-stack").value = state.table_config.starting_stack;
  document.getElementById("cpu-count").value = String(state.table_config.cpu_count);
  renderCommunity(state.community_cards);
  renderSeats(state.players);
  renderActionButtons(state);
  renderLogs(state.logs);
  renderHistory(state.history);
  renderHeroWinRate(state);
  if (!freezeCpuPanels) {
    ensureCpuConfigRendered(state.players);
    ensureCpuMultiSlotsRendered();
  }
  setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
  if (!document.getElementById("cpu-match-result").innerHTML) {
    renderCpuMatchResult(null);
  }
}

async function refreshState() {
  try {
    if (requestInFlight) {
      return;
    }
    if (currentState) {
      const human = currentState.players.find((player) => player.is_human);
      if (human && human.is_current_turn) {
        return;
      }
    }
    const active = document.activeElement;
    if (
      active &&
      (
        active.id === "bet-amount" ||
        active.id === "starting-stack" ||
        active.id === "cpu-count" ||
        active.id.startsWith("cpu-path-") ||
        active.id.startsWith("cpu-code-") ||
        active.id.startsWith("cpu-file-") ||
        active.id.startsWith("cpu-multi-file-")
      )
    ) {
      return;
    }
    // file input は再描画で選択内容が消えるため、選択中は自動更新を止めます。
    if (hasPendingFileSelection()) {
      return;
    }
    const state = await apiFetch(stateUrlWithReveal());
    renderState(state);
  } catch (error) {
    console.error(error);
  }
}

document.getElementById("new-hand-btn").addEventListener("click", async () => {
  if (requestInFlight) return;
  try {
    revealFoldedHands = false;
    const state = await apiFetch(newHandUrl, { method: "POST" });
    renderState(state);
  } catch (error) {
    const message =
      error.message || "このハンドはまだ進行中です。アクションを完了してから次のゲームへ進んでください。";
    document.getElementById("table-message").textContent = message;
    alert(error.message);
  }
});

document.getElementById("reset-table-btn").addEventListener("click", async () => {
  if (requestInFlight) return;
  try {
    revealFoldedHands = false;
    const state = await apiFetch(resetTableUrl, { method: "POST" });
    renderState(state);
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("apply-setup-btn").addEventListener("click", async () => {
  if (requestInFlight) return;
  try {
    const startingStack = Number(document.getElementById("starting-stack").value);
    const cpuCount = Number(document.getElementById("cpu-count").value);
    const state = await apiFetch(configureTableUrl, {
      method: "POST",
      body: JSON.stringify({ starting_stack: startingStack, cpu_count: cpuCount }),
    });
    revealFoldedHands = false;
    renderState(state);
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("reveal-folded-btn").addEventListener("click", async () => {
  if (requestInFlight) return;
  revealFoldedHands = !revealFoldedHands;
  await refreshState();
});

async function pollCpuMultiJob(jobId) {
  // 長時間の自己対戦はサーバー側で走らせ、ここから進捗だけを取りに行きます。
  try {
    const job = await apiFetch(`${cpuMultiJobBaseUrl}/${jobId}`);
    renderCpuMatchProgress(job);
    if (job.latest_snapshot) {
      renderCpuReplaySnapshot(job.latest_snapshot);
    }

    if (job.status === "completed") {
      setCpuMultiRunning(false);
      setFreezeCpuPanels(false);
      stopCpuMultiPolling();
      cpuMultiJobId = null;
      renderCpuMatchResult(job.result);
      return;
    }

    if (job.status === "failed") {
      setCpuMultiRunning(false);
      setFreezeCpuPanels(false);
      stopCpuMultiPolling();
      cpuMultiJobId = null;
      cpuMultiSelectionStatus = `Run failed: ${job.error || job.message}`;
      cpuMultiSelectionTone = "error";
      setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
      alert(job.error || job.message || "CPU self-play failed.");
      return;
    }

    cpuMultiJobPollHandle = setTimeout(() => pollCpuMultiJob(jobId), 1000);
  } catch (error) {
    setCpuMultiRunning(false);
    setFreezeCpuPanels(false);
    stopCpuMultiPolling();
    cpuMultiJobId = null;
    cpuMultiSelectionStatus = `Progress load failed: ${error.message}`;
    cpuMultiSelectionTone = "error";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    alert(error.message);
  }
}

document.getElementById("run-cpu-multi-btn").addEventListener("click", async () => {
  if (requestInFlight || cpuMultiJobId) return;
  try {
    const slotsWithFiles = cpuMultiSlots.filter((slot) => slot.file);
    const totalPlayers = slotsWithFiles.reduce((sum, slot) => sum + slot.count, 0);
    if (slotsWithFiles.length === 0) {
      throw new Error("Select at least one .py file.");
    }
    if (totalPlayers < 2) {
      throw new Error("Set at least two total CPU players.");
    }
    if (totalPlayers > 9) {
      throw new Error("CPU multiplayer supports at most 9 players.");
    }

    setCpuMultiRunning(true);
    setFreezeCpuPanels(true);
    stopCpuMultiPolling();
    cpuMultiSelectionStatus = `Uploading ${slotsWithFiles.length} strategy files...`;
    cpuMultiSelectionTone = "muted";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    document.getElementById("cpu-selfplay-summary").textContent = "自己対戦を準備中です。アップロード後にバックグラウンド実行へ切り替わります。";
    document.getElementById("download-strategy-btn").disabled = true;
    lastStrategyTable = null;

    const uploadedPaths = [];
    for (const slot of slotsWithFiles) {
      const uploaded = await uploadCpuFile(slot.file, { strategyFile: slot.strategyFile });
      for (let index = 0; index < slot.count; index += 1) {
        uploadedPaths.push(uploaded.uploaded_cpu_path);
      }
      slot.label = `Uploaded: ${slot.file.name} x${slot.count}`;
      setUploadStatus(`cpu-multi-slot-status-${slot.id}`, slot.label, "success");
    }

    cpuMultiSelectionStatus = `Uploaded ${slotsWithFiles.length} strategies for ${uploadedPaths.length} seats.`;
    cpuMultiSelectionTone = "success";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);

    const hands = Number(document.getElementById("cpu-multi-hands").value);
    const exportStrategyPath = document.getElementById("cpu-multi-export").value.trim();
    // ライブ再生は小さいジョブでは有用ですが、大きいジョブでは
    // 実行時間を安定させるため自動的に無効化します。
    const liveReplay = hands <= 200;
    renderCpuMatchProgress({
      status: "running",
      completed_hands: 0,
      total_hands: hands,
      percent: 0,
      message: liveReplay
        ? "CPU self-play is running. Live replay updates every few moments."
        : "CPU self-play is running. Live replay is disabled for large runs to keep execution fast.",
      leaderboard_preview: [],
    });

    const job = await apiFetch(cpuMultiStartUrl, {
      method: "POST",
      body: JSON.stringify({
        cpu_paths: uploadedPaths,
        hands,
        starting_stack: Number(document.getElementById("starting-stack").value),
        export_strategy_path: exportStrategyPath || null,
        live_replay: liveReplay,
      }),
    });

    cpuMultiJobId = job.job_id;
    pollCpuMultiJob(job.job_id);
  } catch (error) {
    setCpuMultiRunning(false);
    setFreezeCpuPanels(false);
    stopCpuMultiPolling();
    cpuMultiJobId = null;
    cpuMultiSelectionStatus = `Run failed: ${error.message}`;
    cpuMultiSelectionTone = "error";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    alert(error.message);
  }
});

document.getElementById("add-cpu-slot-btn").addEventListener("click", () => {
  setFreezeCpuPanels(true);
  const nextId = cpuMultiSlots.length ? Math.max(...cpuMultiSlots.map((slot) => slot.id)) + 1 : 1;
  cpuMultiSlots.push({ id: nextId, file: null, strategyFile: null, count: 1, label: "No file selected." });
  cpuMultiSlotsSignature = null;
  renderCpuMultiSlots();
  updateCpuMultiSelectionSummary();
  setFreezeCpuPanels(false);
});

document.getElementById("remove-cpu-slot-btn").addEventListener("click", () => {
  if (cpuMultiSlots.length <= 2) return;
  setFreezeCpuPanels(true);
  cpuMultiSlots.pop();
  cpuMultiSlotsSignature = null;
  renderCpuMultiSlots();
  updateCpuMultiSelectionSummary();
  setFreezeCpuPanels(false);
});

document.getElementById("download-strategy-btn").addEventListener("click", downloadStrategyTable);

renderCpuMatchProgress(null);
renderCpuReplaySnapshot(null);
setCpuMultiRunning(false);
refreshState();
setInterval(refreshState, 6000);
