(() => {
  const STORAGE_KEY = "x4_cloud_api_token";
  let lastStatus = null;

  const $ = (id) => document.getElementById(id);

  function getToken() {
    return sessionStorage.getItem(STORAGE_KEY) || "";
  }

  function authHeaders() {
    const t = getToken();
    const h = { "Content-Type": "application/json" };
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      ...options,
      headers: { ...authHeaders(), ...(options.headers || {}) },
    });
    if (resp.status === 401) {
      $("authBanner").classList.remove("hidden");
      throw new Error("Unauthorized — enter X4_API_TOKEN");
    }
    $("authBanner").classList.add("hidden");
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.reason || data.error || `HTTP ${resp.status}`);
    }
    return data;
  }

  function updateClocks() {
    const now = new Date();
    const utc = now.toISOString().slice(11, 19);
    const pkt = new Date(now.getTime() + 5 * 3600 * 1000).toISOString().slice(11, 19);
    $("clockDisplay").textContent = `${utc} UTC · ${pkt} PKT`;
  }

  function fmtMoney(val, ccy = "USD") {
    const n = Number(val || 0);
    return `${ccy === "USD" ? "$" : ""}${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${ccy !== "USD" ? " " + ccy : ""}`;
  }

  function renderStatus(s) {
    lastStatus = s;
    const acc = s.account;
    const eng = s.engine;
    const pol = s.policy;

    $("modeBadge").textContent = `${acc.broker_mode.toUpperCase()} · ${acc.trade_mode.toUpperCase()}`;
    const connBadge = $("connBadge");
    if (acc.connected) {
      connBadge.textContent = acc.broker_mode === "mt5_bridge"
        ? `BRIDGE ONLINE (${acc.heartbeat_age_sec ?? 0}s)`
        : "BROKER CONNECTED";
      connBadge.className = "pill pill-ok";
    } else {
      connBadge.textContent = "DISCONNECTED / WAITING";
      connBadge.className = "pill pill-warn";
    }

    $("kpiEquity").textContent = fmtMoney(acc.equity, acc.currency);
    $("kpiAccountMeta").textContent = `${acc.account_label} @ ${acc.server}`;
    $("kpiBalance").textContent = fmtMoney(acc.balance, acc.currency);
    $("kpiMargin").textContent = `Free Margin: ${fmtMoney(acc.margin_free, acc.currency)}`;
    $("kpiDrawdown").textContent = `${acc.daily_drawdown_pct.toFixed(2)}% / ${acc.overall_drawdown_pct.toFixed(2)}%`;
    $("kpiPolicyLimits").textContent = `Limits: ${pol.daily_loss_limit_pct}% daily · ${pol.max_overall_drawdown_pct}% max · ${pol.max_risk_per_trade_pct}%/trade`;

    const kpiEng = $("kpiEngineState");
    if (eng.kill_switch_active) {
      kpiEng.textContent = "KILL SWITCH ACTIVE";
      kpiEng.style.color = "#ef4444";
    } else if (eng.running) {
      kpiEng.textContent = `RUNNING (${eng.strategy})`;
      kpiEng.style.color = "#22c55e";
    } else {
      kpiEng.textContent = "SAFE-HALTED";
      kpiEng.style.color = "#f59e0b";
    }
    $("kpiEngineMeta").textContent = `Cycles: ${eng.cycle_count} · Real Lock: ${pol.allow_real && pol.real_execution_unlock ? "UNLOCKED" : "HARD-LOCKED"}`;
    $("policyHash").textContent = `Policy ${pol.policy_id}@${pol.policy_version} (${pol.policy_hash})`;
    $("engineSummary").textContent = eng.kill_switch_active
      ? `HALTED: ${eng.kill_switch_reason}`
      : eng.last_summary;

    const killBtn = $("killSwitchBtn");
    killBtn.textContent = eng.kill_switch_active ? "Reset Kill Switch" : "Emergency Kill Switch";

    if (s.bridge) {
      $("bridgeBox").classList.remove("hidden");
      $("bridgeStatusText").textContent =
        `Connected=${s.bridge.connected} | Heartbeat=${s.bridge.heartbeat_age_sec ?? "never"}s | PendingCmds=${s.bridge.pending_commands} | Agent=${s.bridge.agent_version || "n/a"}`;
    } else {
      $("bridgeBox").classList.add("hidden");
    }

    // Quotes table
    const qBody = $("quotesBody");
    qBody.innerHTML = "";
    const symbols = Object.keys(s.quotes || {}).sort();
    if (!symbols.length) {
      qBody.innerHTML = `<tr><td colspan="5" class="muted">No quotes received yet.</td></tr>`;
    } else {
      for (const sym of symbols) {
        const q = s.quotes[sym];
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="mono"><strong>${sym}</strong></td><td class="mono">${q.bid}</td><td class="mono">${q.ask}</td><td class="mono">${q.spread_pips}</td><td class="mono">${q.age_sec}s</td>`;
        qBody.appendChild(tr);
      }
    }

    // Positions table
    const pBody = $("positionsBody");
    pBody.innerHTML = "";
    const positions = s.positions || [];
    $("posCount").textContent = `${positions.length} open`;
    if (!positions.length) {
      pBody.innerHTML = `<tr><td colspan="8" class="muted">No open positions.</td></tr>`;
    } else {
      for (const p of positions) {
        const pnlClass = Number(p.profit) >= 0 ? "pos-pnl-up" : "pos-pnl-down";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">#${p.ticket}</td>
          <td class="mono">${p.symbol}</td>
          <td>${p.side}</td>
          <td class="mono">${p.volume}</td>
          <td class="mono">${p.open_price}</td>
          <td class="mono">${p.sl} / ${p.tp}</td>
          <td class="mono ${pnlClass}">${Number(p.profit).toFixed(2)}</td>
          <td><button class="btn btn-danger btn-xs" data-close-ticket="${p.ticket}">Close</button></td>`;
        pBody.appendChild(tr);
      }
    }

    // Orders table
    const oBody = $("ordersBody");
    oBody.innerHTML = "";
    const orders = s.orders || [];
    if (!orders.length) {
      oBody.innerHTML = `<tr><td colspan="6" class="muted">No orders recorded yet.</td></tr>`;
    } else {
      for (const o of orders.slice(0, 12)) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="mono">${o.client_order_id}</td><td class="mono">${o.symbol}</td><td>${o.side}</td><td class="mono">${o.volume}</td><td class="mono">${o.status}</td><td>${o.strategy_id || "-"}</td>`;
        oBody.appendChild(tr);
      }
    }

    // Audit table
    $("auditIntegrity").textContent = s.audit_chain_valid ? "CHAIN VERIFIED ✓" : "CHAIN INVALID ✗";
    const aBody = $("auditBody");
    aBody.innerHTML = "";
    for (const a of (s.audit_recent || []).slice(0, 10)) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${(a.ts_utc || "").slice(11, 19)}</td><td>${a.actor}</td><td class="mono">${a.action}</td><td class="mono muted">${(a.entry_hash || "").slice(0, 10)}</td>`;
      aBody.appendChild(tr);
    }
  }

  async function refresh() {
    try {
      const data = await api("/api/status");
      renderStatus(data);
    } catch (err) {
      $("engineSummary").textContent = err.message;
    }
  }

  $("authBtn").addEventListener("click", () => $("authBanner").classList.toggle("hidden"));
  $("saveTokenBtn").addEventListener("click", () => {
    sessionStorage.setItem(STORAGE_KEY, $("tokenInput").value.trim());
    $("tokenInput").value = "";
    refresh();
  });

  $("startEngineBtn").addEventListener("click", async () => {
    try {
      await api("/api/engine/start", {
        method: "POST",
        body: JSON.stringify({ strategy: $("strategySelect").value }),
      });
      refresh();
    } catch (err) {
      $("engineSummary").textContent = `Start failed: ${err.message}`;
    }
  });

  $("stopEngineBtn").addEventListener("click", async () => {
    try {
      await api("/api/engine/stop", { method: "POST", body: "{}" });
      refresh();
    } catch (err) {
      $("engineSummary").textContent = `Stop failed: ${err.message}`;
    }
  });

  $("killSwitchBtn").addEventListener("click", async () => {
    const active = !(lastStatus && lastStatus.engine && lastStatus.engine.kill_switch_active);
    try {
      await api("/api/engine/kill-switch", {
        method: "POST",
        body: JSON.stringify({ active, reason: active ? "Operator dashboard kill switch" : "Operator reset" }),
      });
      refresh();
    } catch (err) {
      $("engineSummary").textContent = `Kill switch error: ${err.message}`;
    }
  });

  $("fillPresetBtn").addEventListener("click", () => {
    if (!lastStatus || !lastStatus.quotes) return;
    const sym = $("orderSymbol").value;
    const side = $("orderSide").value;
    const q = lastStatus.quotes[sym];
    if (!q) return;
    const pip = sym.includes("XAU") ? 0.1 : (sym.includes("JPY") ? 0.01 : 0.0001);
    const digits = sym.includes("XAU") ? 2 : (sym.includes("JPY") ? 3 : 5);
    const entry = side === "BUY" ? q.ask : q.bid;
    const slDist = pip * 25;
    const tpDist = pip * 45;
    $("orderSl").value = (side === "BUY" ? entry - slDist : entry + slDist).toFixed(digits);
    $("orderTp").value = (side === "BUY" ? entry + tpDist : entry - tpDist).toFixed(digits);
  });

  $("orderForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fb = $("orderFeedback");
    fb.textContent = "Validating order against Risk Policy...";
    try {
      const payload = {
        symbol: $("orderSymbol").value,
        side: $("orderSide").value,
        sl: $("orderSl").value ? Number($("orderSl").value) : null,
        tp: $("orderTp").value ? Number($("orderTp").value) : null,
        volume: $("orderVol").value ? Number($("orderVol").value) : null,
      };
      const res = await api("/api/orders", { method: "POST", body: JSON.stringify(payload) });
      fb.textContent = `Approved (${res.approved_volume} lots, est risk $${res.estimated_risk_usd}) → ${res.receipt.status}: ${res.receipt.message}`;
      refresh();
    } catch (err) {
      fb.textContent = `Blocked: ${err.message}`;
    }
  });

  $("positionsBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-close-ticket]");
    if (!btn) return;
    const ticket = btn.getAttribute("data-close-ticket");
    try {
      await api(`/api/positions/${ticket}/close`, { method: "POST", body: "{}" });
      refresh();
    } catch (err) {
      $("engineSummary").textContent = `Close #${ticket} failed: ${err.message}`;
    }
  });

  updateClocks();
  setInterval(updateClocks, 1000);
  refresh();
  setInterval(refresh, 4000);
})();
