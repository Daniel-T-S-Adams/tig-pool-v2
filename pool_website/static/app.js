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
        <td>${fmtNum(r.nonces_24h)}</td>
        <td>${r.share_pct}%</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (_) {
    tbody.innerHTML = '<tr><td colspan="4" class="loading">Could not load leaderboard</td></tr>';
  }
}
