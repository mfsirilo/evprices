// Autonomia (km cheios) <-> consumo (kWh/100 km) dentro de um mesmo <form>: bateria / consumo * 100 = autonomia.
// Os campos se identificam por data-sync="batt|range|cons"; quem foi digitado por último manda.
document.querySelectorAll('form').forEach(f => {
  const g = k => f.querySelector(`[data-sync="${k}"]`);
  const batt = g('batt'), range = g('range'), cons = g('cons');
  if (!batt || !range || !cons) return;
  const v = el => parseFloat(String(el.value).replace(',', '.')) || 0;
  range.addEventListener('input', () => { if (v(batt) && v(range)) cons.value = (v(batt) / v(range) * 100).toFixed(1); });
  cons.addEventListener('input',  () => { if (v(batt) && v(cons))  range.value = Math.round(v(batt) / v(cons) * 100); });
  batt.addEventListener('input',  () => { if (v(batt) && v(cons))  range.value = Math.round(v(batt) / v(cons) * 100); });
});
