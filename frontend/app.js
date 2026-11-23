/* G-Sec Duration Engine front end. React 18 + htm, talks to the FastAPI app on the same origin. */
(function () {
  const { useState, useEffect, useMemo, useRef, useCallback } = React;
  const html = htm.bind(React.createElement);
  const { GroupedBars, LineChart, Histogram, DBar } = window.Charts;

  /* key tenors come from GET /config (engine setting key_tenors); set in App before pages render */
  let KEYS = ["2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "30Y", "40Y"];
  let KEY_T = [2, 3, 5, 7, 10, 15, 30, 40];
  const kcol = (t) => "r" + String(t);                       // history columns: r2, r15, ...
  const keyMarkers = () => KEY_T.map((t) => ({ x: t, label: t + "Y" }));
  /* a shape given at 2/5/10/30Y, linearly interpolated (flat outside) onto the configured keys */
  const shapeAt = (anchors) => {
    const A = [2, 5, 10, 30];
    return KEY_T.map((t) => {
      if (t <= A[0]) return anchors[0];
      if (t >= A[3]) return anchors[3];
      const i = A.findIndex((a, j) => t >= a && t <= A[j + 1]);
      return Math.round(anchors[i] + (anchors[i + 1] - anchors[i]) * (t - A[i]) / (A[i + 1] - A[i]));
    });
  };
  const SLOTS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
  const API = window.GSEC_API || "";
  const CURVES = { ccil: "CCIL NS", own: "Own Svensson (FBIL)" };

  // ---------------------------------------------------------------- utilities
  async function api(path, opts = {}) {
    const init = { ...opts, headers: { ...(opts.headers || {}) } };
    if (opts.json !== undefined) {
      init.method = init.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    }
    const r = await fetch(API + path, init);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      let msg = body.detail;
      if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
      const err = new Error(msg || `HTTP ${r.status}`);
      err.status = r.status; err.body = body;
      throw err;
    }
    return body;
  }

  const nf = {};
  const fmtN = (v, d = 2) => {
    if (v == null || !isFinite(v)) return "–";
    nf[d] = nf[d] || new Intl.NumberFormat("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
    return nf[d].format(v);
  };
  const cr = (inr, d = 2) => fmtN(inr / 1e7, d);            // Rs crore
  const lakh = (inr, d = 2) => fmtN(inr / 1e5, d);          // Rs lakh
  const signed = (s) => (s.startsWith("-") || s === "–" ? s : "+" + s);
  const pnlClass = (v) => (v > 0 ? "gain-t" : v < 0 ? "loss-t" : "");
  const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const store = {
    get: (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  function useApi(fn, deps) {
    const [state, setState] = useState({ data: null, error: null, loading: true });
    const seq = useRef(0);
    const load = useCallback(() => {
      const id = ++seq.current;
      setState((s) => ({ ...s, loading: true, error: null }));
      return fn().then(
        (data) => id === seq.current && setState({ data, error: null, loading: false }),
        (error) => id === seq.current && setState((s) => ({ data: s.data, error, loading: false })));
    }, deps);
    useEffect(() => { load(); }, [load]);
    return { ...state, reload: load };
  }

  // ---------------------------------------------------------------- shared bits
  const Card = ({ title, sub, right, children }) => html`<section className="card">
    ${(title || right) && html`<div className="card-head"><div>
      ${title && html`<h2>${title}</h2>`}${sub && html`<p className="sub">${sub}</p>`}</div>${right}</div>`}
    ${children}</section>`;

  const Tile = ({ label, value, hint, tone }) => html`<div className="tile">
    <div className="label">${label}</div><div className=${"value " + (tone || "")}>${value}</div>
    ${hint && html`<div className="hint">${hint}</div>`}</div>`;

  const ErrorBox = ({ error }) => error && html`<div className="banner crit" role="alert">
    <span className="icon">✕</span><span>${error.message}</span></div>`;

  const Flags = ({ flags }) => flags && flags.length > 0 && html`<div className="banner warn" role="status">
    <span className="icon">⚠</span><ul>${flags.map((f, i) => html`<li key=${i}>${f}</li>`)}</ul></div>`;

  /* the page files are read fresh from disk, but Python code only loads when the server starts */
  const StaleServer = () => html`<div className="banner crit" role="alert"><span className="icon">✕</span><div>
    <b>The API server is running older engine code than this page.</b> Restart it: press Ctrl+C in the
    terminal running uvicorn, then run <code>python -m uvicorn gsec_engine.api:app</code> again
    (add <code>--reload</code> to pick up code changes automatically).</div></div>`;

  const Loading = ({ on }) => on && html`<span className="spinner" aria-live="polite">Working…</span>`;

  function Table({ columns, rows, total, maxHeight }) {
    return html`<div className="table-wrap" style=${maxHeight ? { maxHeight, overflowY: "auto" } : null}>
      <table><thead><tr>${columns.map((c) => html`<th key=${c.key} className=${c.n ? "n" : ""}>${c.label}</th>`)}</tr></thead>
      <tbody>
        ${rows.map((r, i) => html`<tr key=${r._key || i}>${columns.map((c) => {
          const v = c.render ? c.render(r) : r[c.key];
          return html`<td key=${c.key} className=${(c.n ? "n " : "") + (c.cls ? c.cls(r) : "")}>${v}</td>`;
        })}</tr>`)}
        ${total && html`<tr className="total">${columns.map((c) => html`<td key=${c.key} className=${c.n ? "n" : ""}>${total[c.key] ?? ""}</td>`)}</tr>`}
      </tbody></table></div>`;
  }

  const Chips = ({ options, value, onChange }) => html`<div className="chips" role="group">
    ${options.map((o) => html`<button key=${o} className="chip" aria-pressed=${value === o}
      onClick=${() => onChange(o)}>${o}</button>`)}</div>`;

  const Field = ({ label, children }) => html`<label className="field">${label}${children}</label>`;

  const PNAME = { b0: "β0", b1: "β1", b2: "β2", b3: "β3", tau: "τ", tau1: "τ1", tau2: "τ2" };

  /* the portfolio priced on both curves; the one in use drives every number on the page */
  function CurveCompare({ cc }) {
    if (!cc.available) return html`<${Card} title="Curve comparison"><p className="sub">Only one curve available: ${cc.reason}</p><//>`;
    const mk = cc.rows.some((r) => r.market_clean != null);
    const tag = (k) => `${CURVES[k]} ${cc.curves[k].date}${k === cc.active ? " · in use" : ""}`;
    return html`<${Card} title="Curve comparison"
        sub=${`Each position priced on both curves (clean, per ₹100). Δ = own − CCIL.${mk ? " Market = File F price for the valuation date (" + cc.rows.find((r) => r.market_source)?.market_source + ")." : ""}`}>
      <${Table} columns=${[
        { key: "pos_id", label: "Pos" }, { key: "name", label: "Security" },
        { key: "cc", label: tag("ccil"), n: true, render: (r) => fmtN(r.ccil_clean, 4) },
        { key: "cy", label: "YTM %", n: true, render: (r) => fmtN(r.ccil_ytm, 4) },
        { key: "oc", label: tag("own"), n: true, render: (r) => fmtN(r.own_clean, 4) },
        { key: "oy", label: "YTM %", n: true, render: (r) => fmtN(r.own_ytm, 4) },
        { key: "dp", label: "Δ paise", n: true, render: (r) => signed(fmtN(r.diff_paise, 1)) },
        { key: "db", label: "Δ bp", n: true, render: (r) => signed(fmtN(r.diff_bp, 1)) },
        ...(mk ? [{ key: "m", label: "Market", n: true, render: (r) => fmtN(r.market_clean, 4) },
                  { key: "cm", label: "CCIL − mkt (paise)", n: true, render: (r) => r.market_clean == null ? "–" : signed(fmtN(r.ccil_vs_market_paise, 1)) },
                  { key: "om", label: "Own − mkt (paise)", n: true, render: (r) => r.market_clean == null ? "–" : signed(fmtN(r.own_vs_market_paise, 1)) }] : []),
        { key: "dm", label: "Δ MV (₹ cr)", n: true, cls: (r) => pnlClass(r.diff_mv_inr), render: (r) => signed(cr(r.diff_mv_inr)) },
      ]} rows=${cc.rows.map((r) => ({ ...r, _key: r.pos_id }))}
        total=${{ pos_id: "Total", cc: "MV " + cr(cc.totals.ccil_mv_inr), oc: "MV " + cr(cc.totals.own_mv_inr), dm: signed(cr(cc.totals.diff_mv_inr)) }} />
    <//>`;
  }

  // ================================================================ Risk (daily run)
  function RiskPage({ p }) {
    const run = useApi(() => api("/run", { json: { date: p.date, interpolation: p.interp, time_basis: p.basis, curve: p.curve,
      write_reports: false, include_pnl: true } }), [p.date, p.interp, p.basis, p.curve]);
    const [group, setGroup] = useState("All");
    const [kb, setKb] = useState("Spot");                    // KRD basis shown in the chart
    const [saved, setSaved] = useState(null);
    const [saving, setSaving] = useState(false);
    const d = run.data;

    const save = async () => {
      setSaving(true); setSaved(null);
      try {
        await api("/run", { json: { date: p.date, interpolation: p.interp, time_basis: p.basis, curve: p.curve, write_reports: true } });
        setSaved(await api(`/reports/${p.date}`));
      } catch (e) { setSaved({ error: e }); }
      setSaving(false);
    };

    if (!d) return html`<${ErrorBox} error=${run.error} /><${Loading} on=${run.loading} />`;
    if (!d.krd_par || !d.curve_comparison) return html`<${StaleServer} />`;
    const r = d.risk, h = d.historical.stats;
    const posNames = d.valuation.map((v) => v.pos_id);
    const showPositions = posNames.length <= 7;
    const par = kb === "Par";
    const krdRows = par ? d.krd_par : d.krd;
    const krdSeries = [
      ...(showPositions ? krdRows.map((k, i) => ({ name: `${k.pos_id} ${d.valuation[i].name}`, color: SLOTS[i],
        values: KEYS.map((key) => k[`KRD_${key}`]) })) : []),
      { name: "Portfolio", color: SLOTS[showPositions ? posNames.length : 0], values: d.kr01_portfolio.map((k) => (par ? k.portfolio_KRD_par : k.portfolio_KRD)) },
    ];
    const groups = ["All", ...new Set(d.scenarios.map((s) => s.group))];
    const scen = d.scenarios.filter((s) => group === "All" || s.group === group);
    const maxAbs = Math.max(...scen.map((s) => Math.abs(s.full_reprice)));
    const pnl = (d.historical.pnl || []).map((x) => x.pnl_inr / 1e7);

    return html`
      <${ErrorBox} error=${run.error} />
      <${Flags} flags=${d.flags} />
      <div className="tiles">
        <${Tile} label="Net market value" value=${"₹" + cr(r.net_mv_inr, 0) + " cr"}
                 hint=${`gross ₹${cr(d.totals.gross_mv_inr, 0)} cr · accrued ₹${cr(d.totals.net_accrued_inr, 1)} cr`} />
        <${Tile} label="Duration (sum of KRDs)" value=${fmtN(r.portfolio_duration, 3)}
                 hint=${`spot basis · par basis ${fmtN(r.portfolio_duration_par, 3)}`} />
        <${Tile} label="Convexity" value=${fmtN(r.portfolio_convexity, 2)} hint=${`convexity term for ±100bp: +₹${cr(0.5 * r.dollar_convexity_inr * 1e-4, 0)} cr`} />
        <${Tile} label="Net KR01" value=${"₹" + lakh(r.net_KR01_total_inr, 0) + " lakh"} hint="loss per +1bp on all key rates" />
        <${Tile} label="1-day VaR 95% / 99%" value=${`₹${cr(h.VaR_95_inr, 0)} / ${cr(h.VaR_99_inr, 0)} cr`}
                 hint=${`historical replay, ${h.n_days} days`} />
        <${Tile} label="Expected shortfall 95% / 99%" value=${`₹${cr(h.ES_95_inr, 0)} / ${cr(h.ES_99_inr, 0)} cr`}
                 hint=${`${h.window_start} to ${h.window_end}`} />
      </div>

      <div className="grid-2">
        <${Card} title="Key-rate duration"
          sub=${par ? `±1bp on semi-annual par yields at each key tenor, zero curve re-bootstrapped (${p.interp} spreading). Sum ${fmtN(r.portfolio_duration_par, 3)}.`
                    : `±1bp on spot (zero) rates at each key tenor, ${p.interp} spreading. Sum ${fmtN(r.portfolio_duration, 3)}.`}
          right=${html`<${Chips} options=${["Spot", "Par"]} value=${kb} onChange=${setKb} />`}>
          <${GroupedBars} categories=${KEYS} series=${krdSeries} yLabel="KRD (years)" fmt=${(v) => fmtN(v, 2)} />
        <//>
        <${Card} title="KR01 by key rate: spot vs par" sub="Portfolio loss for a +1bp move at one key rate, ₹ lakh. Similar total, different split.">
          <${GroupedBars} categories=${KEYS} yLabel="₹ lakh per bp" fmt=${(v) => fmtN(v, 1)}
            series=${[{ name: "Spot-rate bump", color: "var(--s1)", values: d.kr01_portfolio.map((k) => k.net_KR01_inr / 1e5) },
                      { name: "Par-yield bump", color: "var(--s2)", values: d.kr01_portfolio.map((k) => k.net_KR01_par_inr / 1e5) }]} />
          <${Table} columns=${[
            { key: "key_rate", label: "Key rate" },
            { key: "ks", label: "KRD spot", n: true, render: (k) => fmtN(k.portfolio_KRD, 4) },
            { key: "kp", label: "KRD par", n: true, render: (k) => fmtN(k.portfolio_KRD_par, 4) },
            { key: "net", label: "KR01 spot (₹ lakh)", n: true, render: (k) => lakh(k.net_KR01_inr) },
            { key: "netp", label: "KR01 par (₹ lakh)", n: true, render: (k) => lakh(k.net_KR01_par_inr) },
          ]} rows=${d.kr01_portfolio} total=${{ key_rate: "Total", ks: fmtN(r.portfolio_duration, 4), kp: fmtN(r.portfolio_duration_par, 4),
            net: lakh(r.net_KR01_total_inr), netp: lakh(r.net_KR01_total_par_inr) }} />
        <//>
      </div>

      <${Card} title="Valuation" sub=${`Settlement ${d.settlement_date} (T+1). Prices per ₹100 face; values ₹ crore, signed.`}
        right=${html`<div className="row"><button onClick=${save} disabled=${saving}>Save reports</button>
          <${Loading} on=${saving || run.loading} /></div>`}>
        ${saved && (saved.error ? html`<${ErrorBox} error=${saved.error} />` : html`<p className="sub">Saved:
          ${saved.files.map((f) => html`<a key=${f} href=${`${API}/reports/${p.date}/${f}`} target="_blank" style=${{ marginRight: 10 }}>${f}</a>`)}</p>`)}
        <${Table} columns=${[
          { key: "pos_id", label: "Pos" }, { key: "name", label: "Security" }, { key: "direction", label: "Side" },
          { key: "face", label: "Face (₹ cr)", n: true, render: (v) => cr(v.signed_face_inr, 0) },
          { key: "residual_years", label: "Residual (y)", n: true, render: (v) => fmtN(v.residual_years, 2) },
          { key: "clean", label: "Clean", n: true, render: (v) => fmtN(v.clean_price, 4) },
          { key: "acc", label: "Accrued", n: true, render: (v) => fmtN(v.accrued_per100, 4) },
          { key: "dirty", label: "Dirty", n: true, render: (v) => fmtN(v.dirty_price, 4) },
          { key: "ytm", label: "YTM % (s.a.)", n: true, render: (v) => fmtN(v.ytm_semiannual_pct, 4) },
          { key: "mv", label: "MV (₹ cr)", n: true, render: (v) => cr(v.signed_mv_inr) },
          { key: "dur", label: "Duration (spot)", n: true, render: (v) => fmtN(d.krd.find((k) => k.pos_id === v.pos_id).parallel_duration, 3) },
          { key: "durp", label: "Duration (par)", n: true, render: (v) => fmtN(d.krd_par.find((k) => k.pos_id === v.pos_id).parallel_duration, 3) },
          { key: "cx", label: "Convexity", n: true, render: (v) => fmtN(d.convexity.find((k) => k.pos_id === v.pos_id).convexity, 2) },
        ]} rows=${d.valuation} total=${{ pos_id: "Total", mv: cr(d.totals.net_mv_inr),
          ytm: fmtN(d.totals.value_weighted_ytm_pct, 4), dur: fmtN(r.portfolio_duration, 3), durp: fmtN(r.portfolio_duration_par, 3), cx: fmtN(r.portfolio_convexity, 2) }} />
      <//>

      <${CurveCompare} cc=${d.curve_comparison} />

      <${Card} title="Scenarios" sub="Change in portfolio value, ₹ crore. Duration-only, duration + convexity, and full reprice."
        right=${html`<${Chips} options=${groups} value=${group} onChange=${setGroup} />`}>
        <${Table} maxHeight=${520} columns=${[
          { key: "scenario", label: "Scenario" },
          ...KEYS.map((k) => ({ key: k, label: `${k} bp`, n: true, render: (s) => fmtN(s[`move_${k}_bp`], 1) })),
          { key: "dur", label: "Duration only", n: true, render: (s) => signed(cr(s.duration_only)) },
          { key: "dc", label: "+ Convexity", n: true, render: (s) => signed(cr(s.duration_convexity)) },
          { key: "full", label: "Full reprice", n: true, cls: (s) => pnlClass(s.full_reprice), render: (s) => signed(cr(s.full_reprice)) },
          { key: "bar", label: "", render: (s) => html`<${DBar} value=${s.full_reprice} max=${maxAbs} />` },
          { key: "pct", label: "% of MV", n: true, render: (s) => fmtN(s.full_reprice_pct_of_net_mv, 2) },
        ]} rows=${scen.map((s) => ({ ...s, _key: s.scenario }))} />
      <//>

      <div className="grid-2">
        <${Card} title="Historical replay: daily P&L" sub=${`Each day's key-rate changes since ${h.window_start} (CCIL NS history) applied to today's portfolio on the ${CURVES[p.curve]} curve, ₹ crore.`}>
          <${Histogram} values=${pnl} fmt=${(v) => fmtN(v, 0)} xLabel="P&L (₹ crore)"
            markers=${[{ x: -h.VaR_95_inr / 1e7, label: "VaR 95" }, { x: -h.VaR_99_inr / 1e7, label: "VaR 99" }]} />
          <h3 style=${{ fontSize: 13, margin: "12px 0 4px" }}>Worst 5 days</h3>
          <${Table} columns=${[
            { key: "date", label: "Date" },
            ...KEY_T.map((t, i) => ({ key: kcol(t), label: `${KEYS[i]} bp`, n: true, render: (w) => fmtN(w[kcol(t)], 1) })),
            { key: "pnl", label: "P&L (₹ cr)", n: true, cls: (w) => pnlClass(w.pnl_inr), render: (w) => signed(cr(w.pnl_inr)) },
          ]} rows=${d.historical.worst5} />
        <//>
        <${Card} title="PCA exposures" sub="Loss for a 1σ daily move along each principal component, ₹ crore">
          <${Table} columns=${[
            { key: "pc", label: "Component" },
            { key: "ex", label: "Explained", n: true, render: (c) => fmtN(c.explained * 100, 1) + "%" },
            { key: "sig", label: "1σ (bp)", n: true, render: (c) => fmtN(c.sigma_bp, 2) },
            ...KEYS.map((k) => ({ key: k, label: k, n: true, render: (c) => fmtN(c.loadings[k], 3) })),
            { key: "loss", label: "Loss per 1σ", n: true, render: (c) => cr(c.loss_per_1sigma_inr) },
          ]} rows=${d.pc_exposures} />
          <p className="sub" style=${{ marginTop: 12 }}>Curve: ${d.run_log.curve.label} ${d.run_log.curve.date} (${d.run_log.curve.origin}):${" "}
            ${Object.entries(d.run_log.curve.params).map(([k, v]) => `${PNAME[k] || k} ${fmtN(v, 4)}`).join(", ")}.
            Spot ${KEYS.map((k) => `${k} ${fmtN(d.run_log.key_rates_today_pct[k], 3)}%`).join(", ")}.
            Par (s.a.) ${KEYS.map((k) => `${k} ${fmtN(d.run_log.key_par_yields_today_pct[k], 3)}%`).join(", ")}.</p>
        <//>
      </div>`;
  }

  // ================================================================ Key-rate ladder
  const MODES = {
    "Full reprice": (g) => g.full_reprice_cr,
    "Duration only": (g) => g.duration_only_cr,
    "Duration + convexity": (g) => g.duration_convexity_cr,
    "Duration error": (g) => g.duration_only_cr - g.full_reprice_cr,
    "Duration + convexity error": (g) => g.duration_convexity_cr - g.full_reprice_cr,
  };

  function LadderPage({ p }) {
    const [maxBp, setMaxBp] = useState(50);
    const [step, setStep] = useState(5);
    const [mode, setMode] = useState("Full reprice");
    const [space, setSpace] = useState("Spot");
    const q = useApi(() => api(`/ladder?date=${p.date}&max_bp=${maxBp}&step=${step}&interpolation=${p.interp}&time_basis=${encodeURIComponent(p.basis)}&space=${space.toLowerCase()}&curve=${p.curve}`),
      [p.date, p.interp, p.basis, p.curve, maxBp, step, space]);
    const d = q.data;
    const controls = html`<div className="row">
      <${Field} label="Bump"><${Chips} options=${["Spot", "Par"]} value=${space} onChange=${setSpace} /><//>
      <${Field} label="Range ± bp"><input type="number" min="5" max="500" step="5" value=${maxBp}
        onChange=${(e) => e.target.value > 0 && setMaxBp(+e.target.value)} /><//>
      <${Field} label="Step bp"><input type="number" min="1" max="50" value=${step}
        onChange=${(e) => e.target.value > 0 && setStep(+e.target.value)} /><//>
      <${Loading} on=${q.loading} /></div>`;
    if (!d) return html`${controls}<${ErrorBox} error=${q.error} />`;
    if (!d.space) return html`<${StaleServer} />`;

    const n = d.krd.length;
    const pos = n <= 7;
    const bars = (rows, pref, port) => [
      ...(pos ? rows.map((r, i) => ({ name: r.pos_id, color: SLOTS[i], values: KEYS.map((k) => r[`${pref}_${k}`]) })) : []),
      { name: "Portfolio", color: SLOTS[pos ? n : 0], values: KEYS.map((k) => port[k]) },
    ];
    const names = [...KEYS, "Parallel"];
    const series = names.map((k, i) => ({
      name: k, color: k === "Parallel" ? "var(--ink-2)" : SLOTS[i % SLOTS.length], dashed: k === "Parallel",
      points: d.grid_crore.filter((g) => g.key_rate === k).map((g) => ({ x: g.shift_bp, y: MODES[mode](g) })),
    }));
    const shifts = [...new Set(d.grid_crore.map((g) => g.shift_bp))];
    const sumKrc = KEYS.reduce((a, k) => a + d.portfolio_krc[k], 0);

    return html`
      <${ErrorBox} error=${q.error} />
      <${Card} title="Key-rate ladder" sub=${`${space === "Par" ? "Par yields (semi-annual, zero curve re-bootstrapped)" : "Spot rates"}: each key rate bumped alone across ±${maxBp}bp (others unchanged), plus a parallel shift. Curve ${d.curve_label} ${d.curve_date}, ${d.interpolation} spreading, net MV ₹${cr(d.net_mv_inr, 0)} cr.`}
        right=${controls}>
        <div className="row" style=${{ marginBottom: 10 }}>
          <${Chips} options=${Object.keys(MODES)} value=${mode} onChange=${setMode} /></div>
        <${LineChart} series=${series} height=${360} endLabels=${true}
          xLabel="Bump (bp)" yLabel=${mode.includes("error") ? "approx − full reprice (₹ cr)" : "Δ portfolio value (₹ crore)"}
          xFmt=${(v) => (v > 0 ? "+" : "") + fmtN(v, 0)} yFmt=${(v) => fmtN(v, Math.abs(v) < 10 && mode.includes("error") ? 2 : 0)} />
      <//>
      <div className="grid-2">
        <${Card} title="Key-rate duration" sub=${`Sum ${fmtN(d.portfolio_duration, 3)} = parallel duration`}>
          <${GroupedBars} categories=${KEYS} series=${bars(d.krd, "KRD", d.portfolio_krd)} yLabel="KRD (years)" fmt=${(v) => fmtN(v, 3)} />
        <//>
        <${Card} title="Key-rate convexity" sub=${`Sum ${fmtN(sumKrc, 1)} vs parallel convexity ${fmtN(d.portfolio_parallel_convexity, 1)}: the gap is the cross terms between key rates`}>
          <${GroupedBars} categories=${KEYS} series=${bars(d.krc, "KRC", d.portfolio_krc)} yLabel="KRC" fmt=${(v) => fmtN(v, 2)} />
        <//>
      </div>
      <${Card} title="Grid" sub=${`${mode}, ₹ crore`}>
        <${Table} maxHeight=${480} columns=${[
          { key: "s", label: "Bump (bp)", n: true, render: (r) => (r.s > 0 ? "+" : "") + r.s },
          ...names.map((k) => ({ key: k, label: k, n: true, cls: (r) => pnlClass(r[k]), render: (r) => fmtN(r[k], 2) })),
        ]} rows=${shifts.map((s) => {
          const row = { s, _key: s };
          names.forEach((k) => { const g = d.grid_crore.find((x) => x.key_rate === k && x.shift_bp === s); row[k] = MODES[mode](g); });
          return row;
        })} />
      <//>`;
  }

  // ================================================================ Scenario builder
  const PRESETS = {
    "Flat": [0, 0, 0, 0], "Parallel +25": [25, 25, 25, 25], "Parallel −25": [-25, -25, -25, -25],
    "Bear steepener": [10, 20, 30, 35], "Bear flattener": [35, 30, 20, 10],
    "Bull steepener": [-35, -30, -20, -10], "Bull flattener": [-10, -20, -30, -35],
    "Belly +20": [0, 20, 20, 0],
  };

  function ScenarioPage({ p }) {
    const [moves, setMoves] = useState(() => KEYS.map(() => 25));
    const [space, setSpace] = useState("Spot");
    const [res, setRes] = useState(null);
    const [err, setErr] = useState(null);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
      const t = setTimeout(async () => {
        setBusy(true);
        try {
          setRes(await api("/scenarios/custom", { json: { date: p.date, interpolation: p.interp, time_basis: p.basis, curve: p.curve,
            space: space.toLowerCase(),
            moves_bp: Object.fromEntries(KEYS.map((k, i) => [k, moves[i]])) } }));
          setErr(null);
        } catch (e) { setErr(e); }
        setBusy(false);
      }, 250);
      return () => clearTimeout(t);
    }, [moves, space, p.date, p.interp, p.basis, p.curve]);
    const setOne = (i, v) => setMoves((m) => m.map((x, j) => (j === i ? (isFinite(v) ? v : 0) : x)));

    return html`
      <${ErrorBox} error=${err} />
      <div className="grid-2">
        <${Card} title="Key-rate moves" sub=${space === "Par"
            ? "Moves are par-yield moves (bp); the zero curve is re-bootstrapped from the shocked par curve."
            : "Moves are spot (zero) rate moves (bp), spread across all cash-flow tenors."}
          right=${html`<${Loading} on=${busy} />`}>
          <div className="row" style=${{ marginBottom: 10 }}>
            <${Field} label="Moves apply to"><${Chips} options=${["Spot", "Par"]} value=${space} onChange=${setSpace} /><//></div>
          <div className="chips" style=${{ marginBottom: 14 }}>${Object.entries(PRESETS).map(([k, v]) =>
            html`<button key=${k} className="chip" onClick=${() => setMoves(shapeAt(v))}>${k}</button>`)}</div>
          ${KEYS.map((k, i) => html`<div className="slider-row" key=${k}>
            <b>${k}</b>
            <input type="range" min="-200" max="200" step="1" value=${moves[i]} aria-label=${`${k} move in bp`}
              onChange=${(e) => setOne(i, +e.target.value)} />
            <input type="number" step="1" value=${moves[i]} aria-label=${`${k} move in bp`}
              onChange=${(e) => setOne(i, parseFloat(e.target.value))} /></div>`)}
        <//>
        <${Card} title="Shape" sub=${`Key-rate moves, bp (${space.toLowerCase()})`}>
          <${GroupedBars} categories=${KEYS} height=${210} yLabel="bp" fmt=${(v) => fmtN(v, 0)}
            series=${[{ name: "Move", color: "var(--s1)", values: moves }]} />
        <//>
      </div>
      ${res && !res.space && html`<${StaleServer} />`}
      ${res && res.space && html`
        <div className="tiles">
          <${Tile} label="Full reprice" value=${signed(cr(res.full_reprice)) + " cr"} tone=${pnlClass(res.full_reprice).replace("-t", "")}
                   hint=${`${fmtN(res.full_reprice / res.net_mv_inr * 100, 3)}% of net MV`} />
          <${Tile} label="Duration only" value=${signed(cr(res.duration_only)) + " cr"} hint="−Σ KR01 × move" />
          <${Tile} label="Duration + convexity" value=${signed(cr(res.duration_convexity)) + " cr"}
                   hint=${`residual ₹${cr(res.full_reprice - res.duration_convexity, 3)} cr`} />
          <${Tile} label="Convexity gap" value=${signed(cr(res.convexity_gap_inr)) + " cr"} hint="full reprice − duration only" />
        </div>
        ${res.stale_curve && html`<${Flags} flags=${[`Stale curve: using the ${res.curve_label} curve from ${res.curve_date}`]} />`}
        <${Card} title="By position" sub="Full reprice, ₹ crore">
          <${Table} columns=${[
            { key: "pos", label: "Position" },
            { key: "v", label: "Δ value (₹ cr)", n: true, cls: (r) => pnlClass(r.v), render: (r) => signed(cr(r.v)) },
          ]} rows=${Object.entries(res.full_reprice_by_position_inr).map(([pos, v]) => ({ pos, v, _key: pos }))}
             total=${{ pos: "Portfolio", v: signed(cr(res.full_reprice)) }} />
        <//>`}`;
  }

  // ================================================================ Curve & NS input
  const TENORS = Array.from({ length: 160 }, (_, i) => (i + 1) * 0.25);
  const blankNS = (date) => ({ date, b0: "", b1: "", b2: "", tau: "", source: "CCIL", notes: "", correction: false });

  function CurvePage({ p, onChanged }) {
    const [cmp, setCmp] = useState("");
    const latest = useApi(() => api(`/ns/latest?date=${p.date}`), [p.date]);
    const curve = useApi(() => Promise.all([
      api(`/curve?date=${p.date}&tenors=${TENORS.join(",")}`),
      cmp ? api(`/curve?date=${cmp}&tenors=${TENORS.join(",")}`) : Promise.resolve(null),
    ]), [p.date, cmp]);
    const recent = useApi(() => api("/ns?limit=15"), []);
    const [form, setForm] = useState(blankNS(p.date));
    const [check, setCheck] = useState(null);
    const [review, setReview] = useState(null);
    const [msg, setMsg] = useState(null);
    const [busy, setBusy] = useState(false);
    useEffect(() => { setForm((f) => ({ ...f, date: p.date })); }, [p.date]);

    const body = () => ({ ...form, b0: +form.b0, b1: +form.b1, b2: +form.b2, tau: +form.tau });
    const ready = ["b0", "b1", "b2", "tau"].every((k) => form[k] !== "" && isFinite(+form[k]));
    const upd = (k) => (e) => { setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }); setCheck(null); setReview(null); };

    const validate = async () => {
      setBusy(true); setMsg(null);
      try { setCheck(await api("/ns/validate", { json: body() })); } catch (e) { setMsg({ error: e }); }
      setBusy(false);
    };
    const save = async (accept) => {
      setBusy(true); setMsg(null);
      try {
        const r = await api("/ns", { json: { ...body(), accept_review: !!accept } });
        setMsg({ ok: `Stored ${r.stored}. ${r.note}`, warnings: r.warnings });
        setReview(null); setCheck(null); setForm(blankNS(p.date));
        latest.reload(); curve.reload(); recent.reload(); onChanged();
      } catch (e) {
        if (e.status === 409) setReview(e.body.review); else setMsg({ error: e });
      }
      setBusy(false);
    };
    const upload = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      setBusy(true); setMsg(null);
      try {
        const r = await api("/ns/import", { method: "POST", body: fd });
        setMsg({ ok: `Imported ${f.name}: ${Object.entries(r.status_counts).map(([k, v]) => `${v} ${k}`).join(", ")}` });
        latest.reload(); curve.reload(); recent.reload(); onChanged();
      } catch (er) { setMsg({ error: er }); }
      e.target.value = ""; setBusy(false);
    };

    const L = latest.data;
    const [c0, c1] = curve.data || [];
    const series = [];
    if (c0) series.push({ name: `Spot ${c0.curve_date}`, color: "var(--s1)", points: c0.points.map((q) => ({ x: q.tenor, y: q.zero_pct })) });
    if (c0) series.push({ name: `Par (s.a.) ${c0.curve_date}`, color: "var(--s3)",
      points: c0.points.filter((q) => q.tenor >= 0.5).map((q) => ({ x: q.tenor, y: q.par_pct })) });   // no coupons below 6M
    const parAt = (k) => { const q = c0 && c0.points.find((x) => x.tenor === parseFloat(k)); return q ? q.par_pct : null; };
    if (c1) series.push({ name: `Spot ${c1.curve_date}`, color: "var(--s2)", points: c1.points.map((q) => ({ x: q.tenor, y: q.zero_pct })) });

    return html`
      <div className="grid-2">
        <${Card} title="Spot and par curves" sub="Nelson-Siegel spot rates (continuously compounded) and the semi-annual par yields implied by them (%)"
          right=${html`<${Field} label="Compare with"><input type="date" value=${cmp} onChange=${(e) => setCmp(e.target.value)} /><//>`}>
          <${ErrorBox} error=${latest.error || curve.error} />
          ${L && html`<p className="sub">Curve used for ${p.date}: <b>${L.date}</b> (${L.source})
            ${L.stale && html` <span className="badge warn">stale</span>`} · β0 ${L.b0}, β1 ${L.b1}, β2 ${L.b2}, τ ${L.tau}</p>`}
          ${series.length > 0 && html`<${LineChart} series=${series} height=${300} xLabel="Tenor (years)" yLabel="Rate (%)"
            xTicks=${[0, 5, 10, 15, 20, 25, 30, 35, 40]} markers=${keyMarkers()}
            xFmt=${(v) => fmtN(v, v % 1 ? 2 : 0)} yFmt=${(v) => fmtN(v, 3)} />`}
          ${L && html`<${Table} columns=${[{ key: "b", label: "Key rate" },
              ...KEYS.map((k) => ({ key: k, label: k, n: true, render: (r) => fmtN(r[k], 4) + "%" }))]}
            rows=${[{ b: "Spot (cont.)", ...L.key_rates_pct, _key: "s" },
                    ...(c0 ? [{ b: "Par (s.a.)", ...Object.fromEntries(KEYS.map((k) => [k, parAt(k)])), _key: "p" }] : [])]} />`}
        <//>
        <${Card} title="Enter today's NS parameters" sub="Stage B: validated before storing. Large moves (>25bp at a key rate) need review.">
          <div className="form-grid">
            <${Field} label="Date"><input type="date" value=${form.date} onChange=${upd("date")} /><//>
            ${["b0", "b1", "b2", "tau"].map((k) => html`<${Field} key=${k} label=${{ b0: "β0", b1: "β1", b2: "β2", tau: "τ" }[k]}>
              <input type="number" step="0.0001" value=${form[k]} onChange=${upd(k)} /><//>`)}
            <${Field} label="Source"><input value=${form.source} onChange=${upd("source")} /><//>
            <${Field} label="Notes"><input value=${form.notes} onChange=${upd("notes")} /><//>
            <label className="field" style=${{ flexDirection: "row", alignItems: "center", gap: 6 }}>
              <input type="checkbox" checked=${form.correction} onChange=${upd("correction")} /> Correction row</label>
          </div>
          <div className="row" style=${{ marginTop: 12 }}>
            <button onClick=${validate} disabled=${!ready || busy}>Validate</button>
            <button className="primary" onClick=${() => save(false)} disabled=${!ready || busy}>Save</button>
            <label className="field" style=${{ marginLeft: "auto" }}>Bulk import (.csv/.xlsx)
              <input type="file" accept=".csv,.xlsx" onChange=${upload} disabled=${busy} /></label>
            <${Loading} on=${busy} />
          </div>
          <div style=${{ display: "grid", gap: 8, marginTop: 12 }}>
            ${check && html`<div className=${"banner " + (check.ok && !check.review.length ? "ok" : check.ok ? "warn" : "crit")}>
              <span className="icon">${check.ok ? (check.review.length ? "⚠" : "✓") : "✕"}</span>
              <div>${check.ok ? "Valid." : "Rejected."}
                ${check.ok && html` Key rates: ${KEYS.map((k) => `${k} ${fmtN(check.key_rates_pct[k], 4)}%`).join(", ")}`}
                <ul>${[...check.errors, ...check.warnings, ...check.review.map((r) => "Needs review: " + r)]
                  .map((x, i) => html`<li key=${i}>${x}</li>`)}</ul></div></div>`}
            ${review && html`<div className="banner warn"><span className="icon">⚠</span><div>
              <b>Needs review before storing</b><ul>${review.map((r, i) => html`<li key=${i}>${r}</li>`)}</ul>
              <button className="primary" onClick=${() => save(true)} disabled=${busy}>I've checked it: accept and save</button></div></div>`}
            ${msg && msg.error && html`<${ErrorBox} error=${msg.error} />`}
            ${msg && msg.ok && html`<div className="banner ok"><span className="icon">✓</span><div>${msg.ok}
              ${msg.warnings && msg.warnings.length > 0 && html`<ul>${msg.warnings.map((w, i) => html`<li key=${i}>${w}</li>`)}</ul>`}</div></div>`}
          </div>
        <//>
      </div>
      <${Card} title="Recent NS rows" sub="File B, latest 15">
        <${ErrorBox} error=${recent.error} />
        ${recent.data && html`<${Table} maxHeight=${420} columns=${[
          { key: "date", label: "Date" }, { key: "b0", label: "β0", n: true }, { key: "b1", label: "β1", n: true },
          { key: "b2", label: "β2", n: true }, { key: "tau", label: "τ", n: true }, { key: "source", label: "Source" },
          { key: "entered_at", label: "Entered" }, { key: "flags", label: "Flags" }, { key: "notes", label: "Notes" },
        ]} rows=${[...recent.data.rows].reverse().map((r) => ({ ...r, _key: r.date + r.entered_at }))} />`}
      <//>`;
  }

  // ================================================================ Portfolio
  const EMPTY_POS = { pos_id: "", name: "", isin: "", coupon: "", issue_date: "", maturity_date: "",
    coupon_dates: "", direction: "long", size_type: "face", size_inr: "", opened_on: "" };

  function PortfolioPage({ onChanged }) {
    const q = useApi(() => api("/portfolio"), []);
    const [form, setForm] = useState(EMPTY_POS);
    const [editing, setEditing] = useState(null);
    const [msg, setMsg] = useState(null);
    const [busy, setBusy] = useState(false);
    const upd = (k) => (e) => setForm({ ...form, [k]: e.target.value });

    const submit = async (e) => {
      e.preventDefault();
      setBusy(true); setMsg(null);
      const body = { ...form, coupon: +form.coupon, size_inr: +form.size_inr * 1e7, opened_on: form.opened_on || null };
      try {
        const r = editing
          ? await api(`/portfolio/positions/${encodeURIComponent(editing)}`, { method: "PUT", json: body })
          : await api("/portfolio/positions", { json: body });
        setMsg({ ok: `${editing ? "Saved" : "Added"} ${form.pos_id}`, warnings: r.warnings });
        setForm(EMPTY_POS); setEditing(null); q.reload(); onChanged();
      } catch (er) { setMsg({ error: er }); }
      setBusy(false);
    };
    const del = async (id) => {
      if (!window.confirm(`Delete position ${id}? This rewrites the portfolio file.`)) return;
      setBusy(true);
      try { await api(`/portfolio/positions/${encodeURIComponent(id)}`, { method: "DELETE" }); q.reload(); onChanged(); }
      catch (er) { setMsg({ error: er }); }
      setBusy(false);
    };
    const edit = (r) => {
      setEditing(r.pos_id); setMsg(null);
      setForm({ ...EMPTY_POS, ...r, coupon: String(r.coupon), size_inr: String(r.size_inr / 1e7), opened_on: r.opened_on || "" });
    };

    return html`
      <${Card} title="Positions" sub="File C. Sizes in ₹ crore (face or market value). Short positions carry negative value.">
        <${ErrorBox} error=${q.error} />
        ${q.data && html`<${Table} columns=${[
          { key: "pos_id", label: "Pos" }, { key: "name", label: "Security" }, { key: "isin", label: "ISIN" },
          { key: "coupon", label: "Coupon %", n: true }, { key: "issue_date", label: "Issue" },
          { key: "maturity_date", label: "Maturity" }, { key: "coupon_dates", label: "Coupon dates" },
          { key: "direction", label: "Side" }, { key: "size_type", label: "Size type" },
          { key: "size", label: "Size (₹ cr)", n: true, render: (r) => cr(r.size_inr, 0) },
          { key: "act", label: "", render: (r) => html`<span>
            <button className="link" onClick=${() => edit(r)}>Edit</button>
            <button className="link danger" onClick=${() => del(r.pos_id)} disabled=${busy}>Delete</button></span>` },
        ]} rows=${q.data.positions.map((r) => ({ ...r, _key: r.pos_id }))} />`}
      <//>
      <${Card} title=${editing ? `Edit ${editing}` : "Add position"} sub="Coupon dates: two day-month pairs, e.g. 10-Apr/10-Oct or 2 July/2 January (one must be the maturity day).">
        <form onSubmit=${submit}>
          <div className="form-grid">
            <${Field} label="Position ID"><input required value=${form.pos_id} onChange=${upd("pos_id")} disabled=${!!editing} /><//>
            <${Field} label="Name"><input required value=${form.name} onChange=${upd("name")} placeholder="7.18% GS 2033" /><//>
            <${Field} label="ISIN"><input value=${form.isin} onChange=${upd("isin")} /><//>
            <${Field} label="Coupon %"><input required type="number" step="0.0001" value=${form.coupon} onChange=${upd("coupon")} /><//>
            <${Field} label="Issue date"><input required type="date" value=${form.issue_date} onChange=${upd("issue_date")} /><//>
            <${Field} label="Maturity date"><input required type="date" value=${form.maturity_date} onChange=${upd("maturity_date")} /><//>
            <${Field} label="Coupon dates"><input required value=${form.coupon_dates} onChange=${upd("coupon_dates")} placeholder="10-Apr/10-Oct" /><//>
            <${Field} label="Side"><select value=${form.direction} onChange=${upd("direction")}>
              <option value="long">long</option><option value="short">short</option></select><//>
            <${Field} label="Size type"><select value=${form.size_type} onChange=${upd("size_type")}>
              <option value="face">face</option><option value="mv">market value</option></select><//>
            <${Field} label="Size (₹ crore)"><input required type="number" min="0" step="any" value=${form.size_inr} onChange=${upd("size_inr")} /><//>
            <${Field} label="Opened on"><input type="date" value=${form.opened_on} onChange=${upd("opened_on")} /><//>
          </div>
          <div className="row" style=${{ marginTop: 12 }}>
            <button className="primary" type="submit" disabled=${busy}>${editing ? "Save" : "Add"}</button>
            ${editing && html`<button type="button" onClick=${() => { setEditing(null); setForm(EMPTY_POS); }}>Cancel</button>`}
            <${Loading} on=${busy} />
          </div>
        </form>
        <div style=${{ marginTop: 10 }}>
          ${msg && msg.error && html`<${ErrorBox} error=${msg.error} />`}
          ${msg && msg.ok && html`<div className="banner ok"><span className="icon">✓</span><div>${msg.ok}
            ${msg.warnings && msg.warnings.length > 0 && html`<ul>${msg.warnings.map((w, i) => html`<li key=${i}>${w}</li>`)}</ul>`}</div></div>`}
        </div>
      <//>`;
  }

  // ================================================================ Calibration
  function CalibrationPage() {
    const q = useApi(() => api("/calibration"), []);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const recal = async () => {
      setBusy(true); setErr(null);
      try { await api("/calibration", { method: "POST" }); q.reload(); } catch (e) { setErr(e); }
      setBusy(false);
    };
    const c = q.data;
    const right = html`<div className="row"><button onClick=${recal} disabled=${busy}>Recalibrate</button><${Loading} on=${busy} /></div>`;
    if (!c) return html`<${ErrorBox} error=${q.error || err} />${right}`;
    const keyIdx = c.keys.map((k, i) => ({ k, i, label: KEYS[i] }));
    return html`
      <${ErrorBox} error=${err} />
      <${Card} title="Calibration (File D)" right=${right}
        sub=${`Window ${c.window_start} to ${c.window_end}, ${c.n_changes} daily changes. Calibrated ${c.calibrated_at}.`}>
        <div className="grid-2">
          <div>
            <${GroupedBars} categories=${KEYS} height=${220} yLabel="bp / day" fmt=${(v) => fmtN(v, 2)}
              series=${[{ name: "σ", color: "var(--s1)", values: c.keys.map((k) => c.sigma_bp[k]) },
                        { name: "Robust σ (MAD)", color: "var(--s2)", values: c.keys.map((k) => c.robust_sigma_bp[k]) }]} />
          </div>
          <div>
            <h3 style=${{ fontSize: 13, margin: "0 0 6px" }}>Correlation of daily changes</h3>
            <${Table} columns=${[{ key: "label", label: "" }, ...KEYS.map((k, j) => ({ key: k, label: k, n: true,
              render: (r) => html`<span style=${{ display: "inline-block", minWidth: 44, padding: "1px 6px", borderRadius: 4,
                background: `color-mix(in srgb, var(--s1) ${Math.round(Math.abs(c.corr[r.i][j]) * 55)}%, transparent)` }}>
                ${fmtN(c.corr[r.i][j], 3)}</span>` }))]} rows=${keyIdx.map((r) => ({ ...r, _key: r.k }))} />
          </div>
        </div>
      <//>
      <${Card} title="Principal components" sub="Eigenvectors of the covariance of daily key-rate changes (bp)">
        <div className="grid-2">
          <${GroupedBars} categories=${KEYS} height=${240} yLabel="loading" fmt=${(v) => fmtN(v, 3)}
            series=${c.pc_names.slice(0, 3).map((n, j) => ({ name: n, color: SLOTS[j], values: c.loadings.map((row) => row[j]) }))} />
          <${Table} columns=${[
            { key: "n", label: "Component" },
            { key: "ex", label: "Explained", n: true, render: (r) => fmtN(r.ex * 100, 2) + "%" },
            { key: "s", label: "1σ (bp/day)", n: true, render: (r) => fmtN(r.s, 3) },
            ...KEYS.map((k, i) => ({ key: k, label: k, n: true, render: (r) => fmtN(c.loadings[i][r.j], 3) })),
          ]} rows=${c.pc_names.map((n, j) => ({ n, j, ex: c.explained[j], s: c.pc_sigma_bp[j], _key: n }))} />
        </div>
      <//>`;
  }

  // ================================================================ Yield builder (own curve from FBIL prices)
  const SEV = { error: ["crit", "✕"], warn: ["warn", "⚠"], ok: ["ok", "✓"], info: ["info", "i"] };
  const bpCls = (lim) => (v) => (v == null ? "" : Math.abs(v) > lim ? "bad" : "");
  const sgn = (v, d = 1) => (v == null ? "–" : Math.abs(v) < 0.5 * 10 ** -d ? fmtN(0, d) : signed(fmtN(v, d)));   // no "-0.00"

  function YieldBuilderPage({ p, onChanged }) {
    const [pick, setPick] = useState("");                    // stored FBIL date; "" = latest on/before valuation date
    const [view, setView] = useState("Par yields & bonds");
    const [filter, setFilter] = useState("All");
    const [msg, setMsg] = useState(null);
    const [busy, setBusy] = useState(false);
    const q = useApi(() => api(`/yieldbuilder?date=${pick || p.date}`), [pick, p.date]);
    const d = q.data;

    const upload = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      setBusy(true); setMsg(null);
      try {
        const r = await api("/yieldbuilder/import", { method: "POST", body: fd });
        setMsg({ ok: `Imported ${r.imported.file}: FBIL ${r.imported.date}, ${r.imported.bonds} bonds, ${r.imported.market_prices_added} prices added to market prices (File F). Curve fitted and saved.` });
        setPick(r.imported.date); q.reload(); onChanged();
      } catch (er) { setMsg({ error: er }); }
      e.target.value = ""; setBusy(false);
    };

    const head = html`<${Card} title="FBIL input"
        sub="FBIL's daily G-Sec price/yield workbook (sheets G-Sec and Par Yield). The file FBIL serves as .xls is really .xlsx; both names are accepted.">
      <div className="row">
        <${Field} label="Upload FBIL file"><input type="file" accept=".xlsx,.xls" onChange=${upload} disabled=${busy} /><//>
        ${d && d.dates.length > 0 && html`<${Field} label="Stored FBIL date"><select value=${pick} onChange=${(e) => setPick(e.target.value)}>
          <option value="">Latest on/before ${p.date}</option>
          ${d.dates.map((x) => html`<option key=${x} value=${x}>${x}</option>`)}</select><//>`}
        <${Loading} on=${busy || q.loading} />
      </div>
      <div style=${{ display: "grid", gap: 8, marginTop: 10 }}>
        ${msg && msg.error && html`<${ErrorBox} error=${msg.error} />`}
        ${msg && msg.ok && html`<div className="banner ok"><span className="icon">✓</span><span>${msg.ok}</span></div>`}
        ${d && d.stale && html`<div className="banner warn"><span className="icon">⚠</span><span>
          No FBIL file for ${d.requested_date}: showing the latest one, <b>${d.fbil_date}</b>. Upload that day's file for a current curve.</span></div>`}
        ${d && html`<p className="sub">FBIL prices of <b>${d.fbil_date}</b> (${d.source}) · settlement ${d.settlement_date} (T+1) ·
          cash-flow times ${d.time_basis} · zero rates continuously compounded, par yields and YTMs semi-annual.</p>`}
      </div>
    <//>`;
    if (!d) return html`${head}<${ErrorBox} error=${q.error} />`;
    if (!d.findings) return html`<${StaleServer} />`;

    const S = d.stats, sv = d.params.svensson;
    const used = d.bonds.filter((b) => b.used);
    const pt = (b, k) => ({ x: b.residual_years, y: b[k], label: `${fmtN(b.coupon, 2)}% ${b.maturity} (${b.tag})` });
    const curveSeries = view === "Zero rates" ? [
      { name: "Own Svensson zero", color: "var(--s1)", points: d.points.map((r) => ({ x: r.tenor, y: r.zero_svensson })) },
      { name: "Own NS zero", color: "var(--s3)", dashed: true, points: d.points.map((r) => ({ x: r.tenor, y: r.zero_ns })) },
      ...(d.params.ccil ? [{ name: `CCIL NS zero ${d.params.ccil.date}`, color: "var(--s2)", points: d.points.map((r) => ({ x: r.tenor, y: r.zero_ccil })) }] : []),
    ] : [
      { name: "Own Svensson par", color: "var(--s1)", points: d.points.filter((r) => r.tenor >= 0.5).map((r) => ({ x: r.tenor, y: r.par_svensson })) },
      ...(d.params.ccil ? [{ name: "CCIL NS par", color: "var(--s2)", points: d.points.filter((r) => r.tenor >= 0.5).map((r) => ({ x: r.tenor, y: r.par_ccil })) }] : []),
      ...(d.fbil_par.length ? [{ name: "FBIL par", color: "var(--s4)", dashed: true, points: d.fbil_par.map((r) => ({ x: r.tenor, y: r.par_sa })) }] : []),
      { name: "Traded bonds (YTM)", color: "var(--ink)", dots: true, r: 4, points: used.filter((b) => b.tag === "traded").map((b) => pt(b, "ytm")) },
      { name: "Other bonds (YTM)", color: "var(--muted)", dots: true, points: used.filter((b) => b.tag !== "traded").map((b) => pt(b, "ytm")) },
    ];
    const errSeries = [
      { name: "Own Svensson", color: "var(--s1)", dots: true, r: 3.6, points: used.map((b) => pt(b, "err_svensson_bp")) },
      ...(d.params.ccil ? [{ name: "CCIL NS", color: "var(--s2)", dots: true, points: used.filter((b) => b.err_ccil_bp != null).map((b) => pt(b, "err_ccil_bp")) }] : []),
    ];
    const pf = d.portfolio.filter((r) => r.found);
    const sum = (k) => pf.reduce((a, r) => a + (r[k] || 0), 0);
    const hasC = !!d.params.ccil;
    const rows = d.bonds.filter((b) => filter === "All" || (filter === "Traded" ? b.tag === "traded" : filter === "Excluded" ? !b.used
      : Math.abs(b.err_svensson_bp || 0) > 10));

    return html`
      ${head}
      <${Flags} flags=${d.flags} />

      <${Card} title="Portfolio check against FBIL" sub=${`Each position re-priced at FBIL's date ${d.fbil_date}: static data, accrued and price→yield first, then valuation on each curve. Differences are model − FBIL.`}>
        <div className="findings">${d.findings.map((f, i) => { const [cls, ic] = SEV[f.severity] || SEV.info;
          return html`<div key=${i} className=${"banner " + cls}><span className="icon">${ic}</span><span>${f.text}</span></div>`; })}</div>
        <${Table} columns=${[
          { key: "pos_id", label: "Pos" }, { key: "name", label: "Security" }, { key: "isin", label: "ISIN" },
          { key: "fp", label: "FBIL price", n: true, render: (r) => fmtN(r.fbil_price, 4) },
          { key: "fy", label: "FBIL YTM %", n: true, render: (r) => fmtN(r.fbil_ytm, 4) },
          { key: "acc", label: "Accrued", n: true, render: (r) => fmtN(r.accrued, 4) },
          { key: "chk", label: "Price→yield check (bp)", n: true, cls: (r) => bpCls(0.5)(r.ytm_check_bp) || "good", render: (r) => sgn(r.ytm_check_bp, 2) },
          ...(hasC ? [
            { key: "cc", label: "CCIL price", n: true, render: (r) => fmtN(r.ccil_clean, 4) },
            { key: "cp", label: "CCIL Δ paise", n: true, cls: (r) => bpCls(25)(r.ccil_diff_paise), render: (r) => sgn(r.ccil_diff_paise) },
            { key: "cb", label: "CCIL Δ bp", n: true, cls: (r) => bpCls(2)(r.ccil_diff_bp), render: (r) => sgn(r.ccil_diff_bp) }] : []),
          { key: "sc", label: "Own price", n: true, render: (r) => fmtN(r.svensson_clean, 4) },
          { key: "sp", label: "Own Δ paise", n: true, cls: (r) => bpCls(25)(r.svensson_diff_paise), render: (r) => sgn(r.svensson_diff_paise) },
          { key: "sb", label: "Own Δ bp", n: true, cls: (r) => bpCls(2)(r.svensson_diff_bp), render: (r) => sgn(r.svensson_diff_bp) },
          ...(hasC ? [{ key: "cm", label: "MV Δ CCIL (₹ cr)", n: true, cls: (r) => pnlClass(r.ccil_mv_diff_inr), render: (r) => signed(cr(r.ccil_mv_diff_inr)) }] : []),
          { key: "sm", label: "MV Δ own (₹ cr)", n: true, cls: (r) => pnlClass(r.svensson_mv_diff_inr), render: (r) => signed(cr(r.svensson_mv_diff_inr)) },
        ]} rows=${d.portfolio.map((r) => ({ ...r, _key: r.pos_id }))}
          total=${{ pos_id: "Total", cm: signed(cr(sum("ccil_mv_diff_inr"))), sm: signed(cr(sum("svensson_mv_diff_inr"))) }} />
      <//>

      <div className="tiles">
        <${Tile} label="Own Svensson fit" value=${fmtN(S.svensson.rmse_bp, 1) + " bp"} hint=${`RMSE on ${S.svensson.n} bonds · traded ${fmtN(S.svensson.rmse_traded_bp, 1)} bp (${S.svensson.n_traded})`} />
        <${Tile} label="Own NS fit" value=${fmtN(S.ns.rmse_bp, 1) + " bp"} hint=${`4 parameters · traded ${fmtN(S.ns.rmse_traded_bp, 1)} bp`} />
        ${S.ccil && html`<${Tile} label="CCIL NS curve" value=${fmtN(S.ccil.rmse_bp, 1) + " bp"} hint=${`same bonds · traded ${fmtN(S.ccil.rmse_traded_bp, 1)} bp · mean ${sgn(S.ccil.mean_bp)} bp`} />`}
        <${Tile} label="Bonds used" value=${`${S.svensson.n} / ${d.bonds.length}`} hint=${`${d.bonds.length - S.svensson.n} excluded: see "Excluded" in the bond table`} />
      </div>

      <div className="grid-2">
        <${Card} title="Curves" sub=${view === "Zero rates" ? "Zero (spot) rates, %, continuously compounded" : "Semi-annual par yields (%) and FBIL bond YTMs by residual maturity"}
          right=${html`<${Chips} options=${["Par yields & bonds", "Zero rates"]} value=${view} onChange=${setView} />`}>
          <${LineChart} series=${curveSeries} height=${320} xLabel="Maturity (years)" yLabel="Rate (%)"
            xTicks=${[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50]} markers=${keyMarkers()}
            xFmt=${(v) => fmtN(v, v % 1 ? 2 : 0)} yFmt=${(v) => fmtN(v, 3)} />
        <//>
        <${Card} title="Fit errors by bond" sub="Model yield − FBIL yield, bp. Points near zero = the curve reprices that bond.">
          <${LineChart} series=${errSeries} height=${320} xLabel="Maturity (years)" yLabel="Error (bp)"
            xTicks=${[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50]} xFmt=${(v) => fmtN(v, v % 1 ? 2 : 0)} yFmt=${(v) => fmtN(v, 1)} />
        <//>
      </div>

      <div className="grid-2">
        <${Card} title="Key rates" sub="Zero (cont.) and par (s.a.) at the engine's key tenors, %">
          <${Table} columns=${[
            { key: "key_rate", label: "Key" },
            { key: "zs", label: "Own zero", n: true, render: (r) => fmtN(r.zero_svensson, 3) },
            ...(hasC ? [{ key: "zc", label: "CCIL zero", n: true, render: (r) => fmtN(r.zero_ccil, 3) }] : []),
            { key: "ps", label: "Own par", n: true, render: (r) => fmtN(r.par_svensson, 3) },
            ...(hasC ? [{ key: "pc", label: "CCIL par", n: true, render: (r) => fmtN(r.par_ccil, 3) }] : []),
            { key: "pf", label: "FBIL par", n: true, render: (r) => fmtN(r.par_fbil, 2) },
            { key: "d1", label: "Own − FBIL par (bp)", n: true, render: (r) => (r.par_fbil == null ? "–" : sgn((r.par_svensson - r.par_fbil) * 100)) },
            ...(hasC ? [{ key: "d2", label: "CCIL − FBIL par (bp)", n: true, cls: (r) => (r.par_fbil == null ? "" : bpCls(5)((r.par_ccil - r.par_fbil) * 100)),
              render: (r) => (r.par_fbil == null ? "–" : sgn((r.par_ccil - r.par_fbil) * 100)) }] : []),
          ]} rows=${d.key_rates.map((r) => ({ ...r, _key: r.key_rate }))} />
          <h3 style=${{ fontSize: 13, margin: "14px 0 6px" }}>Parameters</h3>
          <div className="kv">
            ${["b0", "b1", "b2", "b3", "tau1", "tau2"].map((k) => html`<div key=${k}><span>Svensson ${{ b0: "β0", b1: "β1", b2: "β2", b3: "β3", tau1: "τ1", tau2: "τ2" }[k]}</span>${fmtN(sv[k], 4)}</div>`)}
          </div>
          <p className="sub" style=${{ marginTop: 8 }}>Own NS: β0 ${fmtN(d.params.ns.b0, 4)}, β1 ${fmtN(d.params.ns.b1, 4)}, β2 ${fmtN(d.params.ns.b2, 4)}, τ ${fmtN(d.params.ns.tau1, 4)}.
            ${hasC && html` CCIL NS ${d.params.ccil.date}: β0 ${d.params.ccil.b0}, β1 ${d.params.ccil.b1}, β2 ${d.params.ccil.b2}, τ ${d.params.ccil.tau}.`}
            Saved to data/G_fitted_curves.csv on import.</p>
        <//>
        <${Card} title="Fit quality by maturity" sub="Mean and RMSE of model − FBIL yield, bp, bonds used in the fit">
          <${Table} columns=${[
            { key: "bucket", label: "Bucket" }, { key: "n", label: "Bonds", n: true },
            { key: "sm", label: "Own mean", n: true, render: (r) => sgn(r.svensson_mean_bp) },
            { key: "sr", label: "Own RMSE", n: true, render: (r) => fmtN(r.svensson_rmse_bp, 1) },
            { key: "nr", label: "NS RMSE", n: true, render: (r) => fmtN(r.ns_rmse_bp, 1) },
            ...(hasC ? [{ key: "cm", label: "CCIL mean", n: true, cls: (r) => bpCls(5)(r.ccil_mean_bp), render: (r) => sgn(r.ccil_mean_bp) },
                        { key: "cr", label: "CCIL RMSE", n: true, render: (r) => fmtN(r.ccil_rmse_bp, 1) }] : []),
          ]} rows=${d.by_bucket.map((r) => ({ ...r, _key: r.bucket }))} />
        <//>
      </div>

      <${Card} title="FBIL bonds" sub="Weights: traded 3, FBIL input point 2, other 1. Price→yield check: engine yield from FBIL's price minus FBIL's yield."
        right=${html`<${Chips} options=${["All", "Traded", "Errors > 10bp", "Excluded"]} value=${filter} onChange=${setFilter} />`}>
        <${Table} maxHeight=${520} columns=${[
          { key: "isin", label: "ISIN" }, { key: "coupon", label: "Coupon %", n: true, render: (b) => fmtN(b.coupon, 2) },
          { key: "maturity", label: "Maturity" }, { key: "ry", label: "Years", n: true, render: (b) => fmtN(b.residual_years, 2) },
          { key: "tag", label: "Status" }, { key: "w", label: "Weight", n: true, render: (b) => fmtN(b.weight, 0) },
          { key: "price", label: "FBIL price", n: true, render: (b) => fmtN(b.price, 4) },
          { key: "ytm", label: "FBIL YTM", n: true, render: (b) => fmtN(b.ytm, 4) },
          { key: "chk", label: "Check (bp)", n: true, cls: (b) => bpCls(0.5)(b.ytm_check_bp), render: (b) => sgn(b.ytm_check_bp, 2) },
          { key: "es", label: "Own err (bp)", n: true, cls: (b) => bpCls(10)(b.err_svensson_bp), render: (b) => sgn(b.err_svensson_bp) },
          ...(hasC ? [{ key: "ec", label: "CCIL err (bp)", n: true, cls: (b) => bpCls(10)(b.err_ccil_bp), render: (b) => sgn(b.err_ccil_bp) }] : []),
          { key: "reason", label: "Excluded because", render: (b) => b.reason || "" },
        ]} rows=${rows.map((b) => ({ ...b, _key: b.isin }))} />
      <//>`;
  }

  // ================================================================ shell
  const TABS = ["Risk", "Key-rate ladder", "Scenario builder", "Curve", "Portfolio", "Calibration", "Yield builder"];

  function App() {
    const [date, setDate] = useState(today());
    const [interp, setInterp] = useState(store.get("gsec-interp", "linear"));
    const [basis, setBasis] = useState(store.get("gsec-basis", "ACT/365"));
    const [curve, setCurve] = useState(store.get("gsec-curve", "ccil") === "own" ? "own" : "ccil");
    const [tab, setTab] = useState(() => {
      const h = decodeURIComponent(location.hash.slice(1));
      return TABS.includes(h) ? h : store.get("gsec-tab", "Risk");
    });
    const [theme, setTheme] = useState(document.documentElement.dataset.theme || "");
    const [rev, setRev] = useState(0);                      // bump to refetch after data changes
    const health = useApi(() => api("/health"), [rev]);
    const conf = useApi(() => api("/config"), []);
    if (conf.data && conf.data.key_labels) { KEYS = conf.data.key_labels; KEY_T = conf.data.key_tenors.map(Number); }

    useEffect(() => { store.set("gsec-tab", tab); history.replaceState(null, "", "#" + encodeURIComponent(tab)); }, [tab]);
    useEffect(() => { store.set("gsec-interp", interp); }, [interp]);
    useEffect(() => { store.set("gsec-basis", basis); }, [basis]);
    useEffect(() => { store.set("gsec-curve", curve); }, [curve]);
    const toggleTheme = () => {
      const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next; store.set("gsec-theme", next); setTheme(next);
    };

    const p = { date, interp, basis, curve };
    const h = health.data;
    const latest = h ? (h.latest_curve ? h.latest_curve[curve] : h.latest_ns) : null;
    const page = {
      "Risk": html`<${RiskPage} key=${rev} p=${p} />`,
      "Key-rate ladder": html`<${LadderPage} key=${rev} p=${p} />`,
      "Scenario builder": html`<${ScenarioPage} key=${rev} p=${p} />`,
      "Curve": html`<${CurvePage} p=${p} onChanged=${() => setRev((r) => r + 1)} />`,
      "Portfolio": html`<${PortfolioPage} onChanged=${() => setRev((r) => r + 1)} />`,
      "Calibration": html`<${CalibrationPage} key=${rev} />`,
      "Yield builder": html`<${YieldBuilderPage} p=${p} onChanged=${() => setRev((r) => r + 1)} />`,
    }[tab];

    return html`
      <header className="top">
        <div className="top-inner">
          <div className="brand">G-Sec Duration Engine
            <small>${h ? html`${h.positions} positions · ${CURVES[curve]} curve ${latest || "none"}
              ${latest !== date && html` <span className="badge warn" title="No curve of this source for the valuation date">curve ≠ valuation date</span>`}
              ${!h.calibrated && html` <span className="badge warn">not calibrated</span>`}` : health.error ? "API unreachable" : "…"}</small>
          </div>
          <div className="controls">
            <${Field} label="Valuation date"><input type="date" value=${date} onChange=${(e) => e.target.value && setDate(e.target.value)} /><//>
            <${Field} label="Bump interpolation"><select value=${interp} onChange=${(e) => setInterp(e.target.value)}>
              <option value="linear">Linear</option><option value="cubic">Natural cubic</option><option value="pchip">PCHIP</option></select><//>
            <${Field} label="Time basis"><select value=${basis} onChange=${(e) => setBasis(e.target.value)}>
              <option>ACT/365</option><option>30/360</option></select><//>
            <${Field} label="Valuation curve"><select value=${curve} onChange=${(e) => setCurve(e.target.value)}>
              <option value="ccil">CCIL NS</option>
              <option value="own" disabled=${!!(h && h.latest_curve && !h.latest_curve.own)}>Own Svensson (FBIL)</option></select><//>
            ${latest && latest !== date && html`<button onClick=${() => setDate(latest)}>Use ${latest}</button>`}
            <button onClick=${toggleTheme} aria-label="Toggle dark mode">◐</button>
          </div>
        </div>
        <nav className="tabs" role="tablist">
          ${TABS.map((t) => html`<button key=${t} role="tab" className="tab" aria-selected=${tab === t} onClick=${() => setTab(t)}>${t}</button>`)}
        </nav>
      </header>
      <main>
        ${health.error && html`<${ErrorBox} error=${new Error("Cannot reach the API. Start it with: uvicorn gsec_engine.api:app")} />`}
        ${conf.data ? page : conf.error ? null : html`<${Loading} on=${true} />`}
      </main>`;
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
