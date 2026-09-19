/* Gráfico de evolução do R$/kWh (degraus) — usado em /evolucao (todas as estações do município)
   e em /station/{id} (uma estação). Lê JSON dos <script type="application/json"> #data, #home e #rng. */
window.evoChart = function () {
  const raw = JSON.parse(document.getElementById('data').textContent);
  const home = JSON.parse(document.getElementById('home').textContent);
  const rng = JSON.parse(document.getElementById('rng').textContent);
  if (!raw.length) return;
  const single = raw.length === 1 && !document.getElementById('shared-y');   // página da estação: sem cabeçalho no painel
  const brl = v => v.toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
  const fmtD = d => d.toLocaleDateString('pt-BR', {day: '2-digit', month: '2-digit', year: '2-digit'});
  const fmtDT = d => d.toLocaleString('pt-BR', {day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit'});
  const colors = ['rgb(var(--s1))', 'rgb(var(--s2))', 'rgb(var(--s3))'];
  const homeColor = i => i ? 'rgb(var(--home2))' : 'rgb(var(--home))';
  const now = Date.now();
  // janela escolhida (De/Para); recorta cada série a ela — tarifa vigente vai até o fim da janela (ou até agora)
  const tMin = rng.start ? Date.parse(rng.start) : Math.min(...raw.flatMap(st => st.series.flatMap(se => se.points.map(p => Date.parse(p.from)))));
  const tMax = Math.min(rng.end ? Date.parse(rng.end) : now, now);
  const clip = pts => pts.map(p => ({...p, t0: Math.max(p.t0, tMin), t1: Math.min(p.t1, tMax)})).filter(p => p.t0 <= p.t1);
  raw.forEach(st => st.series.forEach(se => se.points.forEach(p => { p.t0 = Date.parse(p.from); p.t1 = p.to ? Date.parse(p.to) : now; })));
  const data = raw.map(st => ({...st, series: st.series.map(se => ({...se, points: clip(se.points)})).filter(se => se.points.length)})).filter(st => st.series.length);
  const hidden = raw.length - data.length;
  home.forEach(se => se.points.forEach((p, i) => { p.t0 = Date.parse(p.from + 'T00:00:00'); p.t1 = i === se.points.length - 1 ? now : Date.parse(p.to + 'T23:59:59'); }));
  home.forEach(se => { se.points = clip(se.points); });
  const stPts = data.flatMap(st => st.series.flatMap(se => se.points));
  const rangeEl = document.getElementById('range');
  const panels = document.getElementById('panels');
  if (!data.length) { panels.innerHTML = '<div class="rounded-lg bg-surface-container-low p-4 text-center font-body-sm text-body-sm text-on-surface-variant">Nenhuma tarifa dentro do período escolhido.</div>'; if (rangeEl) rangeEl.textContent = ''; return; }
  const showHome = document.getElementById('show-home'), showHome2 = document.getElementById('show-home2');
  const homeVisible = () => home.filter((se, i) => i === 0 ? (showHome && showHome.checked) : (showHome2 && showHome2.checked));
  const homeClipped = () => homeVisible().filter(se => se.points.length);
  const allPts = () => stPts.concat(homeClipped().flatMap(se => se.points));
  // período curto (< 3 dias): rótulo com hora, senão só data
  const fmtTick = tMax - tMin < 3 * 864e5
    ? d => d.toLocaleString('pt-BR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}).replace(',', '')
    : fmtD;
  if (rangeEl) rangeEl.textContent = hidden ? `${hidden} estação(ões) sem tarifa no período` : '';

  const sharedY = document.getElementById('shared-y') || {checked: true}, fromZero = document.getElementById('from-zero') || {checked: false};
  const tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; document.body.appendChild(tip);

  // escala "bonita": passo 0,25 / 0,5 / 1 conforme a amplitude
  function yDomain(pts) {
    let lo = fromZero.checked ? 0 : Math.min(...pts.map(p => p.price_kwh)), hi = Math.max(...pts.map(p => p.price_kwh));
    const span = Math.max(hi - lo, 0.5), step = span > 4 ? 1 : span > 2 ? 0.5 : 0.25;
    if (!fromZero.checked) lo = Math.floor((lo - step / 2) / step) * step;
    hi = Math.ceil((hi + step / 2) / step) * step;
    const ticks = []; for (let v = lo; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(2));
    return {lo, hi, ticks};
  }
  function xTicks(w, x) {   // no máximo ~1 rótulo por 80 px
    const n = Math.max(2, Math.floor(w / 80)), out = [];
    for (let i = 0; i < n; i++) { const t = tMin + (tMax - tMin) * i / (n - 1); out.push({t, x: x(t)}); }
    return out;
  }
  // tendência da série mais barata dentro da janela: variação entre o primeiro e o último degrau
  function trend(st) {
    const p = st.series[0].points, a = p[0].price_kwh, b = p[p.length - 1].price_kwh;
    if (p.length < 2 || a === b) { const since = Date.parse(p[p.length - 1].from); const d = Math.floor((now - since) / 864e5);
      return `<span class="font-label-sm text-label-sm text-on-surface-variant flex items-center justify-end gap-0.5"><span class="material-symbols-outlined text-[12px]">trending_flat</span>estável${d >= 1 ? ' há ' + d + ' d' : ''}</span>`; }
    const pct = (b - a) / a * 100, up = b > a;
    return `<span class="font-label-sm text-label-sm ${up ? 'text-tertiary' : 'text-secondary'} flex items-center justify-end gap-0.5"><span class="material-symbols-outlined text-[12px]">${up ? 'trending_up' : 'trending_down'}</span>${up ? '+' : ''}${pct.toFixed(1).replace('.', ',')} %</span>`;
  }

  function render() {
    panels.innerHTML = '';
    const hc = homeClipped(), hPts = hc.flatMap(se => se.points);
    const dom = sharedY.checked ? yDomain(allPts()) : null;
    data.forEach(st => {
      const card = document.createElement('div'); card.className = single ? 'evo flex flex-col gap-1' : 'card evo flex flex-col gap-2';
      const pts = st.series.flatMap(se => se.points).concat(hPts), d = dom || yDomain(pts);
      if (!single) card.innerHTML = `<div class="flex items-start justify-between gap-2">
          <div class="min-w-0"><a class="font-display text-headline-md text-on-surface hover:text-primary block truncate" href="/station/${st.station_id}">${st.station}</a>
          <div class="font-body-sm text-body-sm text-on-surface-variant truncate">${st.address || ''}</div></div>
          <div class="text-right shrink-0"><div class="font-display text-price-display-mobile text-primary tabular-nums leading-none">${brl(st.current)}</div>${trend(st)}</div></div>`;
      const box = document.createElement('div'); box.className = 'rounded-lg bg-surface-container-lowest/70 p-1';
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      box.appendChild(svg); card.appendChild(box);
      const lg = document.createElement('div'); lg.className = 'flex flex-wrap gap-x-3 gap-y-0.5 font-label-sm text-label-sm text-on-surface-variant';
      lg.innerHTML = st.series.map((se, i) => `<span class="flex items-center gap-1"><i class="inline-block w-3.5 h-0.5 rounded" style="background:${colors[i]}"></i>${se.label}${se.count > 1 ? ' ×' + se.count : ''}</span>`).join('')
        + hc.map((se, i) => `<span class="flex items-center gap-1"><i class="inline-block w-3.5 h-0.5 rounded" style="background:${homeColor(i)}"></i>${se.label}</span>`).join('');
      card.appendChild(lg);
      panels.appendChild(card);
      draw(svg, st, d, hc);
    });
  }

  function draw(svg, st, d, hc) {
    const W = svg.clientWidth || 300, H = 120, ml = 44, mr = 64, mt = 8, mb = 18;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const x = t => ml + (t - tMin) / Math.max(tMax - tMin, 1) * (W - ml - mr);
    const y = v => mt + (d.hi - v) / (d.hi - d.lo) * (H - mt - mb);
    let g = '';
    d.ticks.forEach(v => { g += `<line class="grid" x1="${ml}" x2="${W - mr}" y1="${y(v)}" y2="${y(v)}" stroke-width="1"/>
                                <text x="${ml - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(2).replace('.', ',')}</text>`; });
    xTicks(W - ml - mr, x).forEach((tk, i, a) => { g += `<text x="${tk.x}" y="${H - 4}" text-anchor="${i === 0 ? 'start' : i === a.length - 1 ? 'end' : 'middle'}">${fmtTick(new Date(tk.t))}</text>`; });
    g += `<line class="axis" x1="${ml}" x2="${W - mr}" y1="${H - mb}" y2="${H - mb}" stroke-width="1"/>`;
    // referência "casa": tracejada, atrás das séries da estação
    hc.forEach((se, i) => {
      const p = se.points; if (!p.length) return;
      let path = `M${x(p[0].t0)},${y(p[0].price_kwh)}`;
      p.forEach((pt, j) => { if (j) path += `L${x(pt.t0)},${y(pt.price_kwh)}`; path += `H${x(pt.t1)}`; });
      const last = p[p.length - 1];
      g += `<path d="${path}" fill="none" stroke="${homeColor(i)}" stroke-width="1.5" stroke-dasharray="3 3" stroke-linejoin="round" stroke-linecap="round"/>
            <text x="${x(last.t1) + 8}" y="${y(last.price_kwh) + 4}">${i ? '☾' : '⌂'} ${last.price_kwh.toFixed(2).replace('.', ',')}</text>`;
    });
    st.series.forEach((se, i) => {
      const c = colors[i], p = se.points;
      let path = `M${x(p[0].t0)},${y(p[0].price_kwh)}`;
      p.forEach((pt, j) => { if (j) path += `L${x(pt.t0)},${y(pt.price_kwh)}`; path += `H${x(pt.t1)}`; });
      const last = p[p.length - 1], ex = x(last.t1), ey = y(last.price_kwh);
      if (i === 0) g += `<path d="${path}V${H - mb}H${x(p[0].t0)}Z" fill="${c}" opacity="0.08"/>`;
      g += `<path d="${path}" fill="none" stroke="${c}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`;
      p.forEach((pt, j) => { if (j) g += `<circle cx="${x(pt.t0)}" cy="${y(pt.price_kwh)}" r="3" fill="${c}"/>`; });
      g += `<circle cx="${ex}" cy="${ey}" r="6" fill="rgb(var(--surface-container-lowest))"/><circle cx="${ex}" cy="${ey}" r="4" fill="${c}"/>`;
      if (i === 0) g += `<text class="end" x="${ex + 8}" y="${ey + 4}">${brl(last.price_kwh)}</text>`;
      if (tMax < now - 60000) g += `<title>preço no fim do período (${fmtDT(new Date(tMax))})</title>`;
    });
    g += `<line id="xh" x1="0" x2="0" y1="${mt}" y2="${H - mb}" stroke="rgb(var(--outline))" stroke-width="1" visibility="hidden"/>`;
    svg.innerHTML = g;
    const xh = svg.querySelector('#xh');
    const move = ev => {
      const r = svg.getBoundingClientRect(), px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      if (px < ml || px > W - mr) { leave(); return; }
      const t = tMin + (px - ml) / (W - ml - mr) * (tMax - tMin);
      xh.setAttribute('x1', px); xh.setAttribute('x2', px); xh.setAttribute('visibility', 'visible');
      const sw = c => `<i class="inline-block w-2.5 h-0.5 align-middle mr-1" style="background:${c}"></i>`;
      const rows = st.series.map((se, i) => { const pt = se.points.find(q => t >= q.t0 && t <= q.t1);
        return `<div>${sw(colors[i])}${se.label}: <b>${pt ? brl(pt.price_kwh) : '—'}</b></div>`; });
      hc.forEach((se, i) => { const pt = se.points.find(q => t >= q.t0 && t <= q.t1); if (!pt) return;
        rows.push(`<div>${sw(homeColor(i))}${se.label}: <b>${brl(pt.price_kwh)}</b>` +
                  `<span class="text-on-surface-variant"> (${pt.tusd_te.toFixed(2).replace('.', ',')} + bandeira ${pt.bandeira || '—'} ${pt.adicional.toFixed(2).replace('.', ',')}; ICMS ${pt.icms_pct}% + PIS/COFINS ${pt.piscofins_pct}%${pt.piscofins_source === 'aproximado' ? '≈' : ''})</span></div>`); });
      tip.innerHTML = `<div class="text-on-surface-variant">${fmtDT(new Date(t))}</div>` + rows.join('');
      tip.hidden = false;
      const cx = ev.touches ? ev.touches[0].clientX : ev.clientX, cy = ev.touches ? ev.touches[0].clientY : ev.clientY;
      tip.style.left = Math.min(cx + 12, window.innerWidth - tip.offsetWidth - 8) + 'px';
      tip.style.top = (cy - tip.offsetHeight - 12 > 0 ? cy - tip.offsetHeight - 12 : cy + 16) + 'px';
    };
    const leave = () => { tip.hidden = true; xh.setAttribute('visibility', 'hidden'); };
    svg.addEventListener('mousemove', move); svg.addEventListener('mouseleave', leave);
    svg.addEventListener('touchstart', move, {passive: true}); svg.addEventListener('touchmove', move, {passive: true});
    svg.addEventListener('touchend', leave);
  }

  [sharedY, fromZero, showHome, showHome2].forEach(el => { if (el && el.addEventListener) el.addEventListener('change', render); });
  let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(render, 150); });
  render();
};
