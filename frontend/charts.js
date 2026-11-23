/* Small SVG chart kit (no chart library): grouped bars, lines with crosshair, histogram.
   Colors come from CSS tokens (var(--s1)...), so light/dark themes switch without re-render. */
(function () {
  const { useState, useRef, useEffect } = React;
  const html = htm.bind(React.createElement);

  function niceTicks(min, max, n = 5) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { min -= 1; max += 1; }
    const raw = (max - min) / n;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const e = raw / mag;
    const step = (e >= 7.5 ? 10 : e >= 3.5 ? 5 : e >= 1.5 ? 2 : 1) * mag;
    const lo = Math.floor(min / step + 1e-9) * step, hi = Math.ceil(max / step - 1e-9) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function linear(d0, d1, r0, r1) {
    const f = (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
    f.invert = (p) => d0 + ((p - r0) / (r1 - r0 || 1)) * (d1 - d0);
    return f;
  }

  function useWidth() {
    const ref = useRef(null);
    const [w, setW] = useState(0);
    useEffect(() => {
      if (!ref.current) return;
      setW(ref.current.clientWidth);
      const ro = new ResizeObserver((e) => setW(Math.floor(e[0].contentRect.width)));
      ro.observe(ref.current);
      return () => ro.disconnect();
    }, []);
    return [ref, w];
  }

  const defaultFmt = (v) => (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

  function Legend({ items }) {
    if (!items || items.length < 2) return null;
    return html`<div className="legend" role="list">
      ${items.map((it) => html`<span key=${it.name} role="listitem">
        <i className=${(it.line ? "line" : "") + (it.dot ? "dot" : "") + (it.dashed ? " dash" : "")}
           style=${{ background: it.color, color: it.color }}></i>${it.name}</span>`)}
    </div>`;
  }

  function Tip({ tip, width }) {
    if (!tip) return null;
    const left = Math.max(0, Math.min(tip.x + 14, width - 200));
    return html`<div className="tip" style=${{ left, top: Math.max(0, tip.y - 10) }}>
      ${tip.title && html`<b>${tip.title}</b>`}
      ${tip.rows.map((r, i) => html`<div className="r" key=${i}>
        <span>${r.color && html`<i style=${{ background: r.color }}></i>`}${r.name}</span><span>${r.value}</span></div>`)}
    </div>`;
  }

  /* tick labels: decimals from the tick step, grouped digits, no "-0" */
  function tickFormatter(ticks) {
    const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
    const dec = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step) - 1e-9)));
    const f = new Intl.NumberFormat("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec });
    return (v) => f.format(Math.abs(v) < step / 1e6 ? 0 : v);
  }

  /* nudge end labels apart so they never overlap (min gap in px) */
  function spread(items, gap = 13) {
    const s = [...items].sort((a, b) => a.y - b.y);
    for (let i = 1; i < s.length; i++) if (s[i].y - s[i - 1].y < gap) s[i].y = s[i - 1].y + gap;
    return s;
  }

  function YAxis({ ticks, y, x0, x1, label, height }) {
    const fmt = tickFormatter(ticks);
    return html`<g>
      ${ticks.map((t) => html`<g key=${t}>
        <line x1=${x0} x2=${x1} y1=${y(t)} y2=${y(t)}
              style=${{ stroke: t === 0 ? "var(--axis)" : "var(--grid)", strokeWidth: t === 0 ? 1 : 0.7 }} />
        <text x=${x0 - 6} y=${y(t)} dy="0.32em" textAnchor="end">${fmt(t)}</text></g>`)}
      ${label && html`<text className="axis-title" transform=${`translate(12,${height / 2}) rotate(-90)`}
                            textAnchor="middle">${label}</text>`}
    </g>`;
  }

  /* bar with a 4px rounded data end, square at the baseline */
  function barPath(x, yBase, yVal, w, r = 4) {
    const h = Math.abs(yBase - yVal);
    r = Math.min(r, w / 2, h);
    if (yVal <= yBase) {
      return `M${x},${yBase}V${yVal + r}Q${x},${yVal} ${x + r},${yVal}H${x + w - r}Q${x + w},${yVal} ${x + w},${yVal + r}V${yBase}Z`;
    }
    return `M${x},${yBase}V${yVal - r}Q${x},${yVal} ${x + r},${yVal}H${x + w - r}Q${x + w},${yVal} ${x + w},${yVal - r}V${yBase}Z`;
  }

  /* categories: ["2Y",...]; series: [{name, color, values}] */
  function GroupedBars({ categories, series, height = 260, fmt = defaultFmt, yLabel, ariaLabel }) {
    const [ref, W] = useWidth();
    const [hover, setHover] = useState(null);
    const m = { l: 56, r: 8, t: 8, b: 26 };
    const all = series.flatMap((s) => s.values).filter((v) => v != null && isFinite(v));
    const ticks = niceTicks(Math.min(0, ...all), Math.max(0, ...all), 5);
    const y = linear(ticks[0], ticks[ticks.length - 1], height - m.b, m.t);
    const band = W > 0 ? (W - m.l - m.r) / categories.length : 0;
    const gw = band * 0.78, bw = Math.max(2, gw / series.length - 2);
    return html`<div className="chart" ref=${ref}>
      <${Legend} items=${series.map((s) => ({ name: s.name, color: s.color }))} />
      ${W > 0 && html`<svg width=${W} height=${height} role="img" aria-label=${ariaLabel || yLabel}>
        <${YAxis} ticks=${ticks} y=${y} x0=${m.l} x1=${W - m.r} fmt=${fmt} label=${yLabel} height=${height} />
        ${categories.map((c, ci) => {
          const gx = m.l + ci * band + (band - gw) / 2;
          return html`<g key=${c}>
            ${hover && hover.ci === ci && html`<rect x=${m.l + ci * band} y=${m.t} width=${band}
                height=${height - m.t - m.b} style=${{ fill: "var(--surface-2)" }} />`}
            ${series.map((s, si) => {
              const v = s.values[ci];
              if (v == null || !isFinite(v) || v === 0) return null;
              return html`<path key=${si} d=${barPath(gx + si * (bw + 2), y(0), y(v), bw)}
                                style=${{ fill: s.color }} />`;
            })}
            <text x=${m.l + ci * band + band / 2} y=${height - 8} textAnchor="middle"
                  style=${{ fill: "var(--ink-2)" }}>${c}</text>
            <rect x=${m.l + ci * band} y=${m.t} width=${band} height=${height - m.t - m.b}
                  fill="transparent"
                  onMouseMove=${(e) => {
                    const b = ref.current.getBoundingClientRect();
                    setHover({ ci, x: e.clientX - b.left, y: e.clientY - b.top });
                  }}
                  onMouseLeave=${() => setHover(null)} />
          </g>`;
        })}
      </svg>`}
      <${Tip} width=${W} tip=${hover && {
        x: hover.x, y: hover.y, title: categories[hover.ci],
        rows: series.map((s) => ({ name: s.name, color: s.color,
          value: s.values[hover.ci] == null ? "–" : fmt(s.values[hover.ci]) })),
      }} />
    </div>`;
  }

  /* series: [{name, color, dashed, dots, points:[{x,y,label?}]}]; dots = scatter, no line;
     markers: [{x, label}] vertical refs */
  function LineChart({ series, height = 300, xFmt = defaultFmt, yFmt = defaultFmt, xLabel, yLabel,
                       xTicks, markers = [], ariaLabel, endLabels = false }) {
    const [ref, W] = useWidth();
    const [hx, setHx] = useState(null);
    const m = { l: 60, r: endLabels ? 96 : 12, t: 10, b: xLabel ? 40 : 24 };
    const pts = series.flatMap((s) => s.points);
    if (!pts.length) return html`<div className="muted">No data</div>`;
    const xs = [...new Set(pts.map((p) => p.x))].sort((a, b) => a - b);
    const yt = niceTicks(Math.min(...pts.map((p) => p.y)), Math.max(...pts.map((p) => p.y)), 5);
    const xt = xTicks || niceTicks(xs[0], xs[xs.length - 1], 8);
    const x = linear(xt[0], xt[xt.length - 1], m.l, W - m.r);
    const y = linear(yt[0], yt[yt.length - 1], height - m.b, m.t);
    const pathOf = (p) => p.map((q, i) => `${i ? "L" : "M"}${x(q.x).toFixed(1)},${y(q.y).toFixed(1)}`).join("");
    const onMove = (e) => {
      const b = ref.current.getBoundingClientRect();
      const xv = x.invert(e.clientX - b.left);
      const near = xs.reduce((a, c) => (Math.abs(c - xv) < Math.abs(a - xv) ? c : a), xs[0]);
      setHx({ x: near, px: e.clientX - b.left, py: e.clientY - b.top });
    };
    const at = (s, xv) => s.points.find((p) => p.x === xv);
    return html`<div className="chart" ref=${ref}>
      <${Legend} items=${series.map((s) => ({ name: s.name, color: s.color, line: !s.dots, dot: s.dots, dashed: s.dashed }))} />
      ${W > 0 && html`<svg width=${W} height=${height} role="img" aria-label=${ariaLabel || yLabel}>
        <${YAxis} ticks=${yt} y=${y} x0=${m.l} x1=${W - m.r} fmt=${yFmt} label=${yLabel} height=${height} />
        ${xt.map((t) => html`<g key=${t}>
          <line x1=${x(t)} x2=${x(t)} y1=${m.t} y2=${height - m.b}
                style=${{ stroke: t === 0 ? "var(--axis)" : "var(--grid)", strokeWidth: t === 0 ? 1 : 0.5 }} />
          <text x=${x(t)} y=${height - m.b + 15} textAnchor="middle">${xFmt(t)}</text></g>`)}
        ${xLabel && html`<text className="axis-title" x=${(m.l + W - m.r) / 2} y=${height - 4}
                               textAnchor="middle">${xLabel}</text>`}
        ${markers.map((mk, i) => html`<g key=${"m" + i}>
          <line x1=${x(mk.x)} x2=${x(mk.x)} y1=${m.t} y2=${height - m.b}
                style=${{ stroke: "var(--ink-2)", strokeDasharray: "3 3", strokeWidth: 1 }} />
          <text x=${x(mk.x) + 4} y=${m.t + 10 + i * 13} style=${{ fill: "var(--ink-2)" }}>${mk.label}</text></g>`)}
        ${series.map((s) => s.dots
          ? html`<g key=${s.name}>${s.points.map((q, i) => html`<circle key=${i} cx=${x(q.x)} cy=${y(q.y)} r=${s.r || 3.2}
               style=${{ fill: s.color, opacity: 0.85 }} />`)}</g>`
          : html`<path key=${s.name} d=${pathOf(s.points)} fill="none"
             style=${{ stroke: s.color, strokeWidth: 2, strokeDasharray: s.dashed ? "6 4" : null,
                       strokeLinejoin: "round", strokeLinecap: "round" }} />`)}
        ${endLabels && spread(series.filter((s) => !s.dots).map((s) => {
          const p = s.points[s.points.length - 1];
          return { name: s.name, x: x(p.x) + 6, y: y(p.y), v: p.y };
        })).map((l) => html`<text key=${"e" + l.name} x=${l.x} y=${l.y} dy="0.32em"
                             style=${{ fill: "var(--ink)" }}>${l.name} ${yFmt(l.v)}</text>`)}
        ${hx && html`<g>
          <line x1=${x(hx.x)} x2=${x(hx.x)} y1=${m.t} y2=${height - m.b}
                style=${{ stroke: "var(--ink-2)", strokeWidth: 1 }} />
          ${series.map((s) => { const p = at(s, hx.x); return p && html`<circle key=${s.name}
              cx=${x(p.x)} cy=${y(p.y)} r="4.5" style=${{ fill: s.color, stroke: "var(--surface)", strokeWidth: 2 }} />`; })}
        </g>`}
        <rect x=${m.l} y=${m.t} width=${Math.max(0, W - m.l - m.r)} height=${height - m.t - m.b}
              fill="transparent" onMouseMove=${onMove} onMouseLeave=${() => setHx(null)} />
      </svg>`}
      <${Tip} width=${W} tip=${hx && {
        x: hx.px, y: hx.py, title: (xLabel ? xLabel.split(" (")[0] + ": " : "") + xFmt(hx.x),
        rows: series.map((s) => ({ s, p: at(s, hx.x) })).filter((r) => r.p)
          .sort((a, b) => b.p.y - a.p.y)
          .map((r) => ({ name: r.p.label ? `${r.s.name}: ${r.p.label}` : r.s.name, color: r.s.color, value: yFmt(r.p.y) })),
      }} />
    </div>`;
  }

  /* values -> histogram; bins left of 0 use the loss pole, right the gain pole */
  function Histogram({ values, bins = 40, height = 240, fmt = defaultFmt, xLabel, markers = [] }) {
    const [ref, W] = useWidth();
    const [hover, setHover] = useState(null);
    const m = { l: 44, r: 12, t: 10, b: 40 };
    if (!values.length) return null;
    const lo = Math.min(...values), hi = Math.max(...values);
    const w = (hi - lo) / bins || 1;
    const counts = new Array(bins).fill(0);
    values.forEach((v) => counts[Math.min(bins - 1, Math.floor((v - lo) / w))]++);
    const xt = niceTicks(lo, hi, 8);
    const x = linear(xt[0], xt[xt.length - 1], m.l, W - m.r);
    const yt = niceTicks(0, Math.max(...counts), 4);
    const y = linear(0, yt[yt.length - 1], height - m.b, m.t);
    return html`<div className="chart" ref=${ref}>
      ${W > 0 && html`<svg width=${W} height=${height} role="img" aria-label="Distribution of daily P&L">
        <${YAxis} ticks=${yt} y=${y} x0=${m.l} x1=${W - m.r} label="days" height=${height} />
        ${xt.map((t) => html`<text key=${t} x=${x(t)} y=${height - m.b + 15} textAnchor="middle">${fmt(t)}</text>`)}
        ${xLabel && html`<text className="axis-title" x=${(m.l + W - m.r) / 2} y=${height - 4} textAnchor="middle">${xLabel}</text>`}
        ${counts.map((c, i) => {
          const a = lo + i * w, b = a + w;
          const bx = x(a) + 1, bw = Math.max(1, x(b) - x(a) - 2);
          return html`<g key=${i}>
            ${c > 0 && html`<path d=${barPath(bx, y(0), y(c), bw, 3)}
                 style=${{ fill: (a + b) / 2 < 0 ? "var(--loss)" : "var(--gain)", opacity: hover === i ? 1 : 0.85 }} />`}
            <rect x=${x(a)} y=${m.t} width=${Math.max(1, x(b) - x(a))} height=${height - m.t - m.b}
                  fill="transparent" onMouseEnter=${() => setHover(i)} onMouseLeave=${() => setHover(null)} />
          </g>`;
        })}
        ${markers.map((mk, i) => html`<g key=${"m" + i}>
          <line x1=${x(mk.x)} x2=${x(mk.x)} y1=${m.t} y2=${height - m.b}
                style=${{ stroke: "var(--ink)", strokeDasharray: "3 3", strokeWidth: 1 }} />
          <text x=${x(mk.x) + 4} y=${m.t + 10 + i * 13} style=${{ fill: "var(--ink)" }}>${mk.label}</text></g>`)}
      </svg>`}
      <${Tip} width=${W} tip=${hover != null && {
        x: x(lo + (hover + 0.5) * w), y: y(counts[hover]) - 30, title: `${fmt(lo + hover * w)} to ${fmt(lo + (hover + 1) * w)}`,
        rows: [{ name: "days", value: counts[hover] }],
      }} />
    </div>`;
  }

  /* diverging inline bar for table cells */
  function DBar({ value, max }) {
    if (value == null || !max) return null;
    const pct = Math.min(50, (Math.abs(value) / max) * 50);
    const neg = value < 0;
    return html`<div className="dbar" aria-hidden="true"><span className=${neg ? "neg" : ""} style=${{
      left: neg ? `${50 - pct}%` : "50%", width: `${pct}%`, background: neg ? "var(--loss)" : "var(--gain)" }}></span></div>`;
  }

  window.Charts = { GroupedBars, LineChart, Histogram, Legend, DBar, niceTicks };
})();
