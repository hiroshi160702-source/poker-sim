const stateUrl = "/api/state";
const newHandUrl = "/api/new-hand";
const actionUrl = "/api/action";
const loadCpuUrl = "/api/load-cpu";
const uploadCpuFileUrl = "/api/upload-cpu-file";
const saveCpuCodeUrl = "/api/save-cpu-code";
const resetTableUrl = "/api/reset-table";
const configureTableUrl = "/api/configure-table";
const cpuMultiMatchUrl = "/api/run-cpu-multiplayer";

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
  { id: 1, file: null, label: "No file selected." },
  { id: 2, file: null, label: "No file selected." },
];
let freezeCpuPanels = false;
let cpuConfigSignature = null;
let cpuMultiSlotsSignature = null;

function setUploadStatus(elementId, message, tone = "muted") {
  const node = document.getElementById(elementId);
  if (!node) return;
  node.className = `upload-status ${tone}`;
  node.textContent = message;
}

function hasPendingFileSelection() {
  const perSeatFiles = Array.from(document.querySelectorAll('input[id^="cpu-file-"]'))
    .some((input) => input.files && input.files.length > 0);
  return perSeatFiles
    || cpuMultiSlots.some((slot) => Boolean(slot.file));
}

function setFreezeCpuPanels(value) {
  freezeCpuPanels = value;
}

async function apiFetch(url, options = {}) {
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
  const formData = new FormData();
  formData.append("file", file);
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

function renderSeats(players) {
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
    node.className = `seat ${player.is_current_turn ? "current-turn" : ""} ${player.folded ? "folded" : ""} seat-${player.seat}`;
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
  document.getElementById("log-list").innerHTML = logs
    .slice()
    .reverse()
    .map((line) => `<div class="list-card">${line}</div>`)
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
    document.getElementById("cpu-match-result").innerHTML = `<div class="list-card">No self-play run yet.</div>`;
    return;
  }
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
    </div>
    ${leaderboard}
    ${seatStats}
    ${recent || `<div class="list-card">No recent result.</div>`}
  `;
}

function renderCpuConfig(players) {
  const cpuPlayers = players.filter((player) => !player.is_human);
  document.getElementById("cpu-config-list").innerHTML = cpuPlayers
    .map(
      (player) => `
        <div class="cpu-config">
          <strong>${player.name}</strong>
          <input id="cpu-path-${player.seat}" type="text" value="${player.cpu_path || ""}" />
          <input id="cpu-file-${player.seat}" type="file" accept=".py" />
          <div id="cpu-upload-status-${player.seat}" class="upload-status muted">${uploadStatusBySeat[player.seat] || "No file selected."}</div>
          <textarea id="cpu-code-${player.seat}" spellcheck="false" placeholder="def decide_action(game_state, player_state, legal_actions):\n    ...">${defaultCpuCode}</textarea>
          <div class="cpu-config-actions">
            <button data-upload-seat="${player.seat}">Upload .py File</button>
            <button data-save-seat="${player.seat}">Save as Python File</button>
          </div>
        </div>
      `
    )
    .join("");

  cpuPlayers.forEach((player) => {
    document
      .getElementById(`cpu-file-${player.seat}`)
      .addEventListener("change", async (event) => {
        const file = event.target.files && event.target.files[0];
        setFreezeCpuPanels(Boolean(file));
        if (!file) {
          uploadStatusBySeat[player.seat] = "No file selected.";
          setUploadStatus(`cpu-upload-status-${player.seat}`, "No file selected.", "muted");
          return;
        }

        try {
          uploadStatusBySeat[player.seat] = `Uploading ${file.name}...`;
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Uploading ${file.name}...`, "muted");
          const nextState = await uploadCpuFile(file, player.seat);
          setFreezeCpuPanels(false);
          uploadStatusBySeat[player.seat] = `Uploaded: ${file.name}`;
          renderState(nextState);
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Uploaded: ${file.name}`, "success");
        } catch (error) {
          setFreezeCpuPanels(false);
          uploadStatusBySeat[player.seat] = `Upload failed: ${error.message}`;
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Upload failed: ${error.message}`, "error");
          alert(error.message);
        }
      });
    document
      .querySelector(`button[data-upload-seat="${player.seat}"]`)
      .addEventListener("click", async () => {
        try {
          const input = document.getElementById(`cpu-file-${player.seat}`);
          const file = input.files && input.files[0];
          if (!file) {
            throw new Error("Select a .py file first.");
          }
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Uploading ${file.name}...`, "muted");
          const nextState = await uploadCpuFile(file, player.seat);
          setFreezeCpuPanels(false);
          uploadStatusBySeat[player.seat] = `Uploaded: ${file.name}`;
          renderState(nextState);
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Uploaded: ${file.name}`, "success");
        } catch (error) {
          uploadStatusBySeat[player.seat] = `Upload failed: ${error.message}`;
          setUploadStatus(`cpu-upload-status-${player.seat}`, `Upload failed: ${error.message}`, "error");
          alert(error.message);
        }
      });
    document
      .querySelector(`button[data-save-seat="${player.seat}"]`)
      .addEventListener("click", async () => {
        const code = document.getElementById(`cpu-code-${player.seat}`).value;
        try {
          const nextState = await apiFetch(saveCpuCodeUrl, {
            method: "POST",
            body: JSON.stringify({ seat: player.seat, code }),
          });
          renderState(nextState);
        } catch (error) {
          alert(error.message);
        }
      });
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
  const container = document.getElementById("cpu-multi-slots");
  container.innerHTML = cpuMultiSlots
    .map(
      (slot, index) => `
        <div class="cpu-config">
          <strong>CPU Slot ${index + 1}</strong>
          <input id="cpu-multi-file-${slot.id}" type="file" accept=".py" />
          <div id="cpu-multi-slot-status-${slot.id}" class="upload-status muted">${slot.label}</div>
        </div>
      `
    )
    .join("");

  cpuMultiSlots.forEach((slot) => {
    document.getElementById(`cpu-multi-file-${slot.id}`).addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      setFreezeCpuPanels(Boolean(file) || cpuMultiSlots.some((entry) => entry.file));
      slot.file = file || null;
      slot.label = file ? `Selected: ${file.name}` : "No file selected.";
      setUploadStatus(`cpu-multi-slot-status-${slot.id}`, slot.label, "muted");
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
  if (selected.length === 0) {
    cpuMultiSelectionStatus = "No files selected.";
    cpuMultiSelectionTone = "muted";
  } else {
    cpuMultiSelectionStatus = `Selected ${selected.length} slots: ${selected.map((slot) => slot.file.name).join(", ")}`;
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

document.getElementById("run-cpu-multi-btn").addEventListener("click", async () => {
  if (requestInFlight) return;
  try {
    const slotsWithFiles = cpuMultiSlots.filter((slot) => slot.file);
    if (slotsWithFiles.length < 2) {
      throw new Error("Select at least two .py files.");
    }
    setFreezeCpuPanels(true);
    cpuMultiSelectionStatus = `Uploading ${slotsWithFiles.length} files...`;
    cpuMultiSelectionTone = "muted";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    const uploaded = [];
    for (const slot of slotsWithFiles) {
      uploaded.push(await uploadCpuFile(slot.file));
      slot.label = `Uploaded: ${slot.file.name}`;
      setUploadStatus(`cpu-multi-slot-status-${slot.id}`, slot.label, "success");
    }
    setFreezeCpuPanels(false);
    cpuMultiSelectionStatus = `Uploaded ${slotsWithFiles.length} files: ${slotsWithFiles.map((slot) => slot.file.name).join(", ")}`;
    cpuMultiSelectionTone = "success";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    const hands = Number(document.getElementById("cpu-multi-hands").value);
    const exportStrategyPath = document.getElementById("cpu-multi-export").value.trim();
    const result = await apiFetch(cpuMultiMatchUrl, {
      method: "POST",
      body: JSON.stringify({
        cpu_paths: uploaded.map((item) => item.uploaded_cpu_path),
        hands,
        starting_stack: Number(document.getElementById("starting-stack").value),
        export_strategy_path: exportStrategyPath || null,
      }),
    });
    renderCpuMatchResult(result);
  } catch (error) {
    setFreezeCpuPanels(false);
    cpuMultiSelectionStatus = `Upload failed: ${error.message}`;
    cpuMultiSelectionTone = "error";
    setUploadStatus("cpu-multi-upload-status", cpuMultiSelectionStatus, cpuMultiSelectionTone);
    alert(error.message);
  }
});

document.getElementById("add-cpu-slot-btn").addEventListener("click", () => {
  setFreezeCpuPanels(true);
  const nextId = cpuMultiSlots.length ? Math.max(...cpuMultiSlots.map((slot) => slot.id)) + 1 : 1;
  cpuMultiSlots.push({ id: nextId, file: null, label: "No file selected." });
  cpuMultiSlotsSignature = null;
  renderCpuMultiSlots();
  updateCpuMultiSelectionSummary();
});

document.getElementById("remove-cpu-slot-btn").addEventListener("click", () => {
  if (cpuMultiSlots.length <= 2) return;
  setFreezeCpuPanels(true);
  cpuMultiSlots.pop();
  cpuMultiSlotsSignature = null;
  renderCpuMultiSlots();
  updateCpuMultiSelectionSummary();
});

refreshState();
setInterval(refreshState, 6000);
