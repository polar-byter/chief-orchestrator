/* Форма удерживает редакцию, по которой пользователь начал составлять предложение. */
(() => {
  const panel = document.getElementById('stage-decisions');
  panel.innerHTML = `
    <h2>Переоценка этапа</h2>
    <p data-current></p><p data-issues class="muted"></p>
    <details data-editor><summary>Подготовить предложение или изменить порядок согласования</summary>
      <form data-form><div class="fields">
        <label>Действие<select name="action"><option value="propose">Предложить оценку</option><option value="accept">Принять предложение</option><option value="reject">Отклонить предложение</option><option value="policy">Изменить полномочия Чифа</option></select></label>
        <label>Автор решения<input name="author" required maxlength="100" autocomplete="off"></label>
        <label>Роль автора<select name="role"><option value="manager">РП</option><option value="analyst">Аналитик</option><option value="engineer">Инженер</option></select></label>
      </div>
      <label data-target hidden>Предложение<select name="proposal"></select></label>
      <label data-replaces hidden>Связь с прежней оценкой<select name="replaces"></select></label>
      <fieldset data-policy hidden><legend>Делегирование решений РП</legend><div class="fields">
        <label>Порядок согласования<select name="mode"><option value="manual">С обсуждением</option><option value="auto">Автоматически в пределах допуска</option></select></label>
        <label>Допуск к утверждённой оценке, чел·ч<input type="number" min="0" step="0.01" name="hours"></label>
        <label>Полномочия действуют по дату<input type="date" name="until"></label>
      </div></fieldset>
      <label>Основание<textarea name="reason" rows="2" maxlength="1500" required></textarea></label>
      <button type="submit">Записать</button>
      <button type="button" data-rebase>Обновить основу предложения</button></form>
    </details>
    <p role="status" aria-live="polite" data-result></p>
    <div data-history></div>`;
  const el = name => panel.querySelector('[data-' + name + ']');
  const form = el('form');
  const fields = form.elements;
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1});
  const hours = value => value == null ? 'нет данных' : number.format(value) + ' чел·ч';
  const statuses = {pending:'На рассмотрении', approved:'Принято', accepted:'Принято', rejected:'Отклонено'};
  const dateFormat = new Intl.DateTimeFormat('ru-RU', {dateStyle:'short', timeStyle:'short'});
  const dateLabel = value => value ? dateFormat.format(new Date(value)) : '';
  let context, formBasis, lastPayload, sending = false, loadId = 0, lastView = '';
  function option(value, label) { return new Option(label, value); }
  function refill(select, choices) {
    const previous = select.value;
    select.replaceChildren(...choices);
    if (Array.from(select.options).some(o => o.value === previous)) select.value = previous;
  }
  function updateFields() {
    const action = fields.action.value;
    el('policy').hidden = action !== 'policy';
    el('target').hidden = !['accept','reject'].includes(action);
    el('replaces').hidden = action !== 'propose' || !context?.decisions?.proposals.length;
    el('rebase').hidden = action !== 'propose';
    fields.proposal.required = !el('target').hidden;
    fields.role.disabled = action !== 'propose';
    if (fields.role.disabled) fields.role.value = 'manager';
    const auto = action === 'policy' && fields.mode.value === 'auto';
    fields.hours.required = fields.until.required = auto;
    fields.hours.disabled = fields.until.disabled = !auto;
  }
  function captureBasis() {
    if (context) formBasis = {fingerprint:context.decisions.forecast.fingerprint, policy_version:context.decisions.policy.version};
  }
  el('editor').addEventListener('toggle', () => { if (el('editor').open) captureBasis(); });
  el('rebase').addEventListener('click', () => {
    captureBasis();
    el('result').textContent = 'Выбрана основа из последней загруженной сводки. Текст предложения сохранён.';
  });
  fields.action.addEventListener('change', updateFields);
  fields.mode.addEventListener('change', updateFields);
  document.addEventListener('stage-view', event => {
    const data = event.detail;
    if (!data?.stage || !data.decisions) { panel.hidden = true; context = null; return; }
    if (!context || context.stage.key !== data.stage.key || context.project.id !== data.project.id) {
      loadId++; form.reset(); el('editor').open = false; el('result').textContent = ''; lastPayload = null; lastView = ''; formBasis = null;
    }
    context = data;
    panel.hidden = false;
    const {forecast, policy, approved, proposals} = data.decisions;
    const signature = JSON.stringify([forecast, policy, approved, proposals]);
    if (signature === lastView) return;
    lastView = signature;
    if (!el('editor').open || !formBasis) captureBasis();
    el('current').textContent = 'Утверждено: ' + hours(approved?.total_hours) + '. Текущий прогноз: ' +
      hours(forecast.total_hours) + '. Полностью оценено фич: ' + forecast.complete_count + ' из ' + forecast.feature_count + '.';
    el('issues').textContent = (policy.mode === 'auto' ? 'Автосогласование, допуск ' + hours(policy.max_delta_hours) +
      ', по ' + policy.valid_until : 'Решение принимает РП') + '. ' + (forecast.issues || []).join(' ');
    const history = el('history');
    history.replaceChildren();
    const pending = [];
    for (const p of proposals) {
      const details = document.createElement('details'), title = document.createElement('summary');
      const status = statuses[p.status] || p.status;
      title.textContent = [status, dateLabel(p.created_at), p.author || 'Чиф', hours(p.total_hours), p.stale ? 'изменились входы' : ''].filter(Boolean).join(' · ');
      const description = document.createElement('p'), decision = document.createElement('p');
      description.textContent = p.reason + ' · Известная часть: ' + hours(p.known_hours) + '. До предложения: ' + hours(p.previous_hours) + '. Изменение: ' + hours(p.delta_hours) + '.';
      decision.textContent = [p.decided_by, dateLabel(p.decided_at), p.method === 'auto' ? 'автоматически' : p.method === 'manual' ? 'вручную' : '', p.decision_reason, ...(p.changed_inputs || [])].filter(Boolean).join(' · ');
      details.append(title, description, decision); history.append(details);
      if (p.status === 'pending') pending.push(option(p.id, p.reason + ' · ' + hours(p.total_hours)));
    }
    refill(fields.proposal, pending);
    refill(fields.replaces, [option('','Новое предложение'), ...proposals.map(p => option(p.id,p.reason + ' · ' + (statuses[p.status] || p.status)))]);
    updateFields();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!context || sending || !form.reportValidity()) return;
    const action = fields.action.value;
    const payload = {stage:context.stage.key, project_id:context.project.id, action, role:fields.role.value,
      author:fields.author.value.trim(), reason:fields.reason.value.trim()};
    if (action === 'propose') {
      payload.fingerprint = formBasis.fingerprint;
      if (fields.replaces.value) payload.replaces = fields.replaces.value;
    } else if (action === 'policy') {
      payload.policy_version = formBasis.policy_version;
      payload.mode = fields.mode.value;
      if (payload.mode === 'auto') {
        payload.max_delta_hours = fields.hours.value;
        payload.valid_until = fields.until.value;
      }
    } else { payload.proposal = fields.proposal.value; }
    const key = JSON.stringify(payload);
    if (!lastPayload || lastPayload.key !== key) lastPayload = {key,id:crypto.randomUUID()};
    payload.request_id = lastPayload.id;
    const currentId = loadId;
    const submit = form.querySelector('button[type=submit]');
    sending = true; submit.disabled = true;
    try {
      const tokenResponse = await fetch('/api/ui');
      if (!tokenResponse.ok) throw new Error('Не удалось подключиться к дашборду.');
      const {token} = await tokenResponse.json();
      const response = await fetch('/api/stage/decision',{method:'POST',
        headers:{'Content-Type':'application/json','X-Dashboard-UI-Token':token},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Не удалось записать решение.');
      if (currentId === loadId) {
        el('result').textContent = 'Записано. ' + (result.decision_reason || '');
        fields.reason.value = ''; el('editor').open = false; lastPayload = null;
      }
    } catch (error) { if (currentId === loadId) el('result').textContent = error.message; }
    finally { sending = false; submit.disabled = false; document.dispatchEvent(new Event('stage-refresh')); }
  });
})();
