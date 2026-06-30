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
