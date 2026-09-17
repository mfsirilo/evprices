// Autonomia (km cheios) <-> consumo (kWh/100 km) dentro de um mesmo <form>: consumo = bateria / autonomia * 100.
// Campos: data-sync="batt|range|cons"; hidden data-sync="source" ('autonomia' | 'consumo') diz qual dos dois o usuário
// digitou — esse vai para o servidor como está; o OUTRO é só exibição (recalculado aqui, rótulo com "=", estilo secundário)
// e continua editável: editá-lo inverte a origem. Nada de arredondar/"corrigir" o que o usuário digitou.
document.querySelectorAll('form').forEach(f => {
  const g = k => f.querySelector(`[data-sync="${k}"]`), lab = k => f.querySelector(`[data-sync-label="${k}"]`);
  const batt = g('batt'), range = g('range'), cons = g('cons'), source = g('source');
  if (!batt || !range || !cons || !source) return;
  const v = el => parseFloat(String(el.value).replace(',', '.')) || 0;
  const fmt = (x, digits) => Number.isFinite(x) ? String(+x.toFixed(digits)) : '';
  function derive() {   // recalcula o campo derivado a partir do digitado
    if (source.value === 'consumo') { if (v(batt) && v(cons)) range.value = fmt(v(batt) / v(cons) * 100, 0); }
    else if (v(batt) && v(range)) cons.value = fmt(v(batt) / v(range) * 100, 1);
  }
  function mark() {     // "=" e estilo secundário acompanham o campo derivado
    const derivedIsRange = source.value === 'consumo';
    range.classList.toggle('derived', derivedIsRange); cons.classList.toggle('derived', !derivedIsRange);
    for (const [k, el] of [['range', lab('range')], ['cons', lab('cons')]]) {
      if (!el) continue;
      const base = el.dataset.text || (el.dataset.text = el.textContent.replace(/^=\s*/, ''));
      el.textContent = ((k === 'range') === derivedIsRange ? '= ' : '') + base;
    }
  }
  range.addEventListener('input', () => { source.value = 'autonomia'; derive(); mark(); });
  cons.addEventListener('input',  () => { source.value = 'consumo';   derive(); mark(); });
  batt.addEventListener('input', derive);
  mark();
});
