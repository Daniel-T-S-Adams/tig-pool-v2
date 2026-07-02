// Shared JS for all pool pages

function fmtNum(n) {
  if (n == null) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

function shortWallet(w) {
  if (!w) return "—";
  return w.slice(0, 6) + "…" + w.slice(-4);
}

async function loadStats() {
  const el = (id) => document.getElementById(id);
  try {
    const resp = await fetch("/api/stats");
    if (!resp.ok) return;
    const d = await resp.json();
    if (el("stat-members")) el("stat-members").textContent = fmtNum(d.active_members);
    if (el("stat-nonces"))  el("stat-nonces").textContent  = fmtNum(d.nonces_last_24h);
    if (el("stat-updates")) el("stat-updates").textContent = fmtNum(d.total_coinbase_updates);
    const feeTxt = d.pool_fee_pct != null ? d.pool_fee_pct + "%" : "—";
    if (el("stat-fee"))  el("stat-fee").textContent  = feeTxt;
    if (el("info-fee"))  el("info-fee").textContent  = feeTxt;
  } catch (_) {}
}

function fmtTig(n) {
  if (n == null) return "—";
  if (n >= 1000) return (n / 1000).toFixed(2) + "K";
  return n.toFixed(2);
}

async function loadEarnings() {
  const el = (id) => document.getElementById(id);
  if (!el("stat-round-tig")) return;
  try {
    const resp = await fetch("/api/earnings");
    if (!resp.ok) return;
    const d = await resp.json();
    if (d.error) return;
    if (d.current_round_tig != null) {
      el("stat-round-tig").textContent = fmtTig(d.current_round_tig) + " TIG";
      if (d.current_round != null)
        el("stat-round-label").textContent = "Round " + d.current_round + " (in progress)";
      const b = d.current_round_benchmarker_tig, s = d.current_round_shared_tig;
      if (b != null && s != null)
        el("stat-round-breakdown").textContent = fmtTig(b) + " benchmarker · " + fmtTig(s) + " delegator";
    }
    if (d.prev_round_tig != null) {
      el("stat-prev-tig").textContent = fmtTig(d.prev_round_tig) + " TIG";
      if (d.prev_round != null)
        el("stat-prev-label").textContent = "Round " + d.prev_round + " (final)";
      const b = d.prev_round_benchmarker_tig, s = d.prev_round_shared_tig;
      if (b != null && s != null)
        el("stat-prev-breakdown").textContent = fmtTig(b) + " benchmarker · " + fmtTig(s) + " delegator";
    }
    if (d.block_reward_tig != null)
      el("stat-block-reward").textContent = "+" + d.block_reward_tig.toFixed(4) + " TIG";
  } catch (_) {}
}

async function checkMemberEarnings() {
  const input = document.getElementById("earnings-wallet-input");
  const status = document.getElementById("earnings-lookup-status");
  const table = document.getElementById("earnings-lookup-table");
  const body = document.getElementById("earnings-lookup-body");
  if (!input || !status || !table || !body) return;

  const wallet = input.value.trim();
  if (!wallet.startsWith("0x") || wallet.length < 10) {
    status.textContent = "Enter a valid wallet address (starts with 0x).";
    table.style.display = "none";
    return;
  }

  status.textContent = "Looking up on-chain earnings…";
  table.style.display = "none";
  body.innerHTML = "";

  try {
    const resp = await fetch("/api/member-earnings?wallet=" + encodeURIComponent(wallet) + "&rounds=12");
    const d = await resp.json();
    if (d.error) {
      status.textContent = "Error: " + d.error;
      return;
    }
    if (!d.history || !d.history.length) {
      status.textContent = "No coinbase earnings found for this wallet in the last 12 rounds.";
      return;
    }
    status.textContent = "Total across " + d.rounds_checked + " round(s): " + fmtTig(d.total_tig_across_rounds) + " TIG";
    d.history.forEach((h) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${h.round}</td>
        <td>${h.final ? "Final" : "In progress"}</td>
        <td>${fmtTig(h.wallet_tig)}</td>
        <td>${h.wallet_pct_of_coinbase}%</td>
        <td>${fmtTig(h.pool_coinbase_total_tig)}</td>
      `;
      body.appendChild(tr);
    });
    table.style.display = "";
  } catch (_) {
    status.textContent = "Could not reach the earnings API. Try again shortly.";
  }
}

async function loadLeaderboard() {
  const tbody = document.getElementById("leaderboard-body");
  if (!tbody) return;
  try {
    const resp = await fetch("/api/leaderboard");
    if (!resp.ok) return;
    const rows = await resp.json();
    tbody.innerHTML = "";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="loading">No contributions yet</td></tr>';
      return;
    }
    rows.forEach((r, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td><a href="/dashboard.html?wallet=${r.wallet_address}">${shortWallet(r.wallet_address)}</a></td>
        <td>${fmtNum(r.nonces_round ?? r.nonces_24h)}</td>
        <td>${r.share_pct}%</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (_) {
    tbody.innerHTML = '<tr><td colspan="4" class="loading">Could not load leaderboard</td></tr>';
  }
}
