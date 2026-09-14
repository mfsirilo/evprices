/* Gráfico de evolução do R$/kWh (degraus) — usado em /evolucao (todas as estações do município)
   e em /station/{id} (uma estação). Lê JSON dos <script type="application/json"> #data, #home e #rng. */
window.evoChart = function () {
  const raw = JSON.parse(document.getElementById('data').textContent);
  const home = JSON.parse(document.getElementById('home').textContent);
  const rng = JSON.parse(document.getElementById('rng').textContent);
  if (!raw.length) return;
  const brl = v => v.toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
  const fmtD = d => d.toLocaleDateString('pt-BR', {day: '2-digit', month: '2-digit', year: '2-digit'});
  const fmtDT = d => d.toLocaleString('pt-BR', {day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit'});
  const colors = ['var(--s1)', 'var(--s2)', 'var(--s3)'];
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
  if (!data.length) { document.getElementById('panels').innerHTML = '<div class="card muted">Nenhuma tarifa dentro do período escolhido.</div>'; if (rangeEl) rangeEl.textContent = ''; return; }
  const showHome = document.getElementById('show-home'), showHome2 = document.getElementById('show-home2');
  const rangeEl = document.getElementById('range');
  const homeVisible = () => home.filter((se, i) => i === 0 ? (showHome && showHome.checked) : (showHome2 && showHome2.checked));
  // só a parte da série de casa dentro da janela do gráfico
  const homeClipped = () => homeVisible().filter(se => se.points.length);
  const allPts = () => stPts.concat(homeClipped().flatMap(se => se.points));
  // período curto (< 3 dias): rótulo com hora, senão só data
  const fmtTick = tMax - tMin < 3 * 864e5
    ? d => d.toLocaleString('pt-BR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}).replace(',', '')
    : fmtD;
  if (rangeEl) rangeEl.textContent = hidden ? `${hidden} estação(ões) sem tarifa no período` : '';

  const sharedY = document.getElementById('shared-y') || {checked: true}, fromZero = document.getElementById('from-zero') || {checked: false};
  const panels = document.getElementById('panels');
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

  function render() {
    panels.innerHTML = '';
    const hc = homeClipped(), hPts = hc.flatMap(se => se.points);
    const dom = sharedY.checked ? yDomain(allPts()) : null;
    data.forEach(st => {
      const card = document.createElement('div'); card.className = 'card panel';
      const pts = st.series.flatMap(se => se.points).concat(hPts), d = dom || yDomain(pts);
      const brand = st.brand || 'tupi';
      card.innerHTML = `<div class="head"><div class="grow" style="min-width:0"><a class="name" href="/station/${st.station_id}">${st.station}</a>
          <div class="muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${st.address || ''}</div></div>
          <div class="cur">${brl(st.current)}<small>/kWh</small></div></div>`;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      card.appendChild(svg);
      if (st.series.length > 1) {
        const lg = document.createElement('div'); lg.className = 'legend';
        lg.innerHTML = st.series.map((se, i) => `<span><i style="background:${colors[i]}"></i>${se.label}${se.count > 1 ? ' ×' + se.count : ''}</span>`).join('');
        card.appendChild(lg);
      } else {
        const lg = document.createElement('div'); lg.className = 'legend';
        lg.textContent = st.series[0].label + (st.series[0].count > 1 ? ' ×' + st.series[0].count : '');
        card.appendChild(lg);
      }
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
    d.ticks.forEach(v => { g += `<line x1="${ml}" x2="${W - mr}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>
                                <text x="${ml - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(2).replace('.', ',')}</text>`; });
    xTicks(W - ml - mr, x).forEach((tk, i, a) => { g += `<text x="${tk.x}" y="${H - 4}" text-anchor="${i === 0 ? 'start' : i === a.length - 1 ? 'end' : 'middle'}">${fmtTick(new Date(tk.t))}</text>`; });
    g += `<line x1="${ml}" x2="${W - mr}" y1="${H - mb}" y2="${H - mb}" stroke="var(--axis)" stroke-width="1"/>`;
    // referência "casa": cinza, atrás das séries da estação
    hc.forEach((se, i) => {
      const p = se.points; if (!p.length) return;
      const c = i === 0 ? 'var(--home)' : 'var(--home2)';
      let path = `M${x(p[0].t0)},${y(p[0].price_kwh)}`;
      p.forEach((pt, j) => { if (j) path += `L${x(pt.t0)},${y(pt.price_kwh)}`; path += `H${x(pt.t1)}`; });
      const last = p[p.length - 1];
      g += `<path d="${path}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            <text x="${x(last.t1) + 8}" y="${y(last.price_kwh) + 4}">${i ? '🌙' : '🏠'} ${last.price_kwh.toFixed(2).replace('.', ',')}</text>`;
    });
    st.series.forEach((se, i) => {
      const c = colors[i], p = se.points;
      let path = `M${x(p[0].t0)},${y(p[0].price_kwh)}`;
      p.forEach((pt, j) => { if (j) path += `L${x(pt.t0)},${y(pt.price_kwh)}`; path += `H${x(pt.t1)}`; });
      const last = p[p.length - 1], ex = x(last.t1), ey = y(last.price_kwh);
      g += `<path d="${path}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            <circle cx="${ex}" cy="${ey}" r="6" fill="var(--card)"/><circle cx="${ex}" cy="${ey}" r="4" fill="${c}"/>`;
      // rótulo direto só no fim da série mais barata (a que define o "atual")
      if (i === 0) g += `<text class="end" x="${ex + 8}" y="${ey + 4}">${brl(last.price_kwh)}</text>`;
      if (tMax < now - 60000) g += `<title>preço no fim do período (${fmtDT(new Date(tMax))})</title>`;
    });
    g += `<line id="xh" x1="0" x2="0" y1="${mt}" y2="${H - mb}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>`;
    svg.innerHTML = g;
    const xh = svg.querySelector('#xh');
    const move = ev => {
      const r = svg.getBoundingClientRect(), px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      if (px < ml || px > W - mr) { leave(); return; }
      const t = tMin + (px - ml) / (W - ml - mr) * (tMax - tMin);
      xh.setAttribute('x1', px); xh.setAttribute('x2', px); xh.setAttribute('visibility', 'visible');
      const rows = st.series.map((se, i) => { const pt = se.points.find(q => t >= q.t0 && t <= q.t1);
        return `<div><i style="display:inline-block;width:10px;height:2px;background:${colors[i]};vertical-align:middle;margin-right:4px"></i>${se.label}: <b>${pt ? brl(pt.price_kwh) : '—'}</b></div>`; });
      hc.forEach((se, i) => { const pt = se.points.find(q => t >= q.t0 && t <= q.t1); if (!pt) return;
        rows.push(`<div><i style="display:inline-block;width:10px;height:2px;background:${i ? 'var(--home2)' : 'var(--home)'};vertical-align:middle;margin-right:4px"></i>${se.label}: <b>${brl(pt.price_kwh)}</b>` +
                  `<span class="muted"> (${pt.tusd_te.toFixed(2).replace('.', ',')} + bandeira ${pt.bandeira || '—'} ${pt.adicional.toFixed(2).replace('.', ',')}; ICMS ${pt.icms_pct}% + PIS/COFINS ${pt.piscofins_pct}%${pt.piscofins_source === 'aproximado' ? '≈' : ''})</span></div>`); });
      tip.innerHTML = `<div class="muted">${fmtDT(new Date(t))}</div>` + rows.join('');
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

