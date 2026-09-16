// Busca de município por digitação: <input data-muni="ID_DO_HIDDEN"> — digita "rio ver" (ou "rio verde/go"),
// aparece a lista "Nome/UF", toca/Enter para escolher. Grava o id no hidden e dispara o evento "muniselect"
// no input (detail = {id, nome, uf}). Sem UF antes: a UF é só uma forma de desempatar nomes repetidos.
(function () {
  const norm = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
  document.querySelectorAll('input[data-muni]').forEach(inp => {
    const hidden = document.getElementById(inp.dataset.muni);
    const box = document.createElement('div'); box.className = 'munilist'; box.hidden = true;
    inp.insertAdjacentElement('afterend', box);
    let items = [], timer = null, active = -1, seq = 0;
    function choose(m) {
      inp.value = `${m.nome}/${m.uf}`; hidden.value = m.id; box.hidden = true; active = -1;
      inp.dispatchEvent(new CustomEvent('muniselect', {detail: m, bubbles: true}));
    }
    function render() {
      box.innerHTML = items.map((m, i) => `<div class="muniopt${i === active ? ' active' : ''}" data-i="${i}">${m.nome}<span class="muted">/${m.uf}${m.monitored ? ' · monitorado' : ''}</span></div>`).join('');
      box.hidden = !items.length;
    }
    async function search() {
      const q = inp.value.trim();
      hidden.value = ''; inp.dispatchEvent(new Event('munichange', {bubbles: true}));
      if (q.length < 2) { items = []; render(); return; }
      const my = ++seq;
      const r = await fetch('/api/municipios?q=' + encodeURIComponent(q) + '&limit=12');
      if (my !== seq || !r.ok) return;
      items = await r.json(); active = items.length ? 0 : -1; render();
      // nome exato (sem acento) e único: já escolhe
      const exact = items.filter(m => norm(m.nome) === norm(q) || norm(`${m.nome}/${m.uf}`) === norm(q));
      if (exact.length === 1) hidden.value = exact[0].id;
    }
    inp.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 150); });
    inp.addEventListener('focus', () => { if (items.length && !hidden.value) box.hidden = false; });
    inp.addEventListener('keydown', e => {
      if (box.hidden) return;
      if (e.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
      else if (e.key === 'Enter') { if (active >= 0) { choose(items[active]); e.preventDefault(); } }
      else if (e.key === 'Escape') { box.hidden = true; }
    });
    box.addEventListener('mousedown', e => { const o = e.target.closest('.muniopt'); if (o) { choose(items[+o.dataset.i]); e.preventDefault(); } });
    document.addEventListener('click', e => { if (!box.contains(e.target) && e.target !== inp) box.hidden = true; });
    inp.setMuni = choose;
  });
})();
