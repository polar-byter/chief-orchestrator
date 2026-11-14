'use strict';

(() => {
  const register = document.getElementById('stage-risks');
  const editor = document.getElementById('stage-document-editor');
  const discussion = document.getElementById('discussion-heading');
  if (!register || !editor || !discussion) return;

  const sections = {requirements:'Требования', bdd:'BDD', nfr:'NFR', effort:'Оценка трудозатрат', risks:'Риски'};
  const hoursFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1});
  const dateFormat = new Intl.DateTimeFormat('ru-RU', {dateStyle:'short', timeStyle:'short'});
  const hours = value => Number.isFinite(value) ? hoursFormat.format(value) + ' чел·ч' : 'Нет данных';
  let view = null;
  let riskSignature = '', discussionSignature = '', documentRoute = null, generation = 0;
  let original = null, conflict = null, pendingRequest = null, saving = false;
  let impactSignature = '';

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text != null) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function link(label, address) {
    const item = node('a', label);
    item.href = address;
    return item;
  }
  function recordName(key) {
    return (view.features.find(item => item.key === key) || view.stage).title;
  }
  function stageURL(changes, anchor = '') {
    const url = new URL(location.href);
    for (const [key, value] of Object.entries(changes)) {
      if (value != null && value !== '') url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    url.hash = anchor;
    return url.href;
  }
  function riskURL(risk, team, section, anchor, thread) {
    return stageURL({feature:risk.key === view.stage.key ? '' : risk.key, revision:null, risk:risk.id,
      section, team, thread, risk_hash: risk.source_hash}, anchor);
  }
  function obsidianLink(address) {
    try {
      const uri = new URL(address);
      if (uri.protocol === 'obsidian:' && uri.hostname === 'open' && uri.searchParams.has('path')) {
        return link('Открыть в Obsidian', uri.href);
      }
    } catch { /* Старая карточка может не содержать ссылки на заметку. */ }
    return null;
  }
  function riskCard(risk, expanded) {
    const item = node('details', null, 'risk-card');
    item.dataset.riskId = risk.id;
    item.open = expanded.has(risk.id);
    const headline = node('summary');
    const name = node('strong', risk.title && risk.title !== risk.risk_id ? risk.risk_id + ' · ' + risk.title : risk.risk_id);
    name.append(node('small', risk.record_title || recordName(risk.key)));
    headline.append(name, node('span', risk.owner || 'Владелец не указан'), node('span', risk.treatment || 'Учёт не задан'));
    item.append(headline);
    const passport = node('dl', null, 'risk-passport');
    function field(label, value, fallback = 'Нет данных') {
      const box = node('div');
      box.append(node('dt', label), node('dd', value == null || value === '' ? fallback : value));
      passport.append(box);
    }
    field('Причина', risk.cause);
    if (risk.event) field('Событие', risk.event);
    if (risk.horizon) field('Горизонт оценки', risk.horizon);
    field('Последствие', risk.impact);
    field('Вероятность', risk.probability, 'Неизвестна');
    field('Основание', risk.basis);
    if (risk.response) field('Способ реагирования', risk.response);
    field('Мера', risk.mitigation);
    if (risk.trigger) field('Сигнал к действию', risk.trigger);
    if (risk.contingency) field('Действие при наступлении', risk.contingency);
    field('Следующая проверка', risk.review_date, 'Не назначена');
    field('Наблюдение', risk.outcome);
    field('Остаточный риск', risk.residual);
    field('Работы', (risk.works || []).join(', '));
    field('Добавка к оценке этапа', hours(risk.added_hours));
    if (risk.counted_at && risk.counted_at !== risk.id) field('Резерв учтён в', risk.counted_at);
    if (risk.common_cause) field('Общая причина', risk.common_cause);
    if (risk.group) field('Общее событие', risk.group);
    if (risk.group_members?.length) field('Связанные риски', risk.group_members.join(', '));
    if (risk.revision) field('Базовая редакция', String(risk.revision));
    const nav = node('div', null, 'inline');
    const threads = view.threads.filter(thread => thread.risk_id === risk.id);
    nav.append(link('Открыть документ риска', riskURL(risk, 'manager', 'risks', 'stage-document-editor')));
    const obsidian = obsidianLink(risk.obsidian_url);
    if (obsidian) nav.append(obsidian);
    nav.append(link(threads.length ? 'Обсуждения · ' + threads.length : 'Обсудить с командой',
      riskURL(risk, 'joint', null, 'discussion-heading', threads.at(-1)?.id)));
    item.append(passport, nav);
    return item;
  }
  function showRegister(query) {
    const ledger = view.risks;
    register.hidden = !ledger;
    if (!ledger) return;
    const feature = query.get('feature'), selected = query.get('risk') || '';
    const history = query.get('revision');
    const signature = JSON.stringify([view.project.id, view.stage.key, ledger, feature, history, selected,
      view.threads.filter(t => t.risk_id).map(t => [t.risk_id, t.id])]);
    if (signature === riskSignature) return;
    const opened = new Set([...register.querySelectorAll('details[open]')].map(d => d.dataset.riskId));
    if (selected) opened.add(selected);
    riskSignature = signature;
    const title = node('h2', 'Реестр рисков');
    register.replaceChildren(title);
    if (history) {
      register.append(node('p', 'Выбрана архивная редакция ' + history + '. Правила и решения на тот момент доступны в сохранённом документе.'),
        link('Открыть риски этой редакции', stageURL({section:'risks'}, 'stage-document-editor')),
        node('span', ' · '), link('Перейти к рабочему реестру', stageURL({section:null, revision:null}, 'stage-risks')));
      return;
    }
    const totals = node('p', null, 'risk-totals');
    totals.append(node('span', 'База этапа · ' + hours(ledger.base_hours)),
      node('span', 'Резерв · ' + hours(ledger.reserve_hours)), node('strong', 'Известная часть · ' + hours(ledger.known_hours)));
    register.append(totals);
    if (ledger.issues?.length) {
      const notes = node('ul', null, 'risk-issues');
      for (const issue of ledger.issues) notes.append(node('li', String(issue)));
      register.append(notes);
    }
    const rows = ledger.rows.filter(r => !feature || r.key === feature);
    if (!rows.length) register.append(node('p', feature ? 'В выбранной фиче пока нет записей о рисках.' : 'В документах этапа пока нет записей о рисках.', 'muted'));
    if (selected && !rows.some(row => row.id === selected)) register.append(node('p', 'Риск ' + selected + ' отсутствует в текущих исходных данных.'));
    for (const risk of rows) register.append(riskCard(risk, opened));
  }

  const topic = node('p', null, 'risk-topic');
  discussion.after(topic);
  function showTopic(query) {
    const id = query.get('team') === 'joint' ? query.get('risk') : '';
    const signature = JSON.stringify([view.project.id, view.stage.key, id, query.get('revision'), query.get('feature')]);
    if (signature === discussionSignature) return;
    discussionSignature = signature;
    topic.replaceChildren();
    topic.hidden = !id;
    if (!id) return;
    topic.append(node('strong', 'Обсуждение риска ' + id), node('span',' · '),
      link('К документу риска', stageURL({team:'manager', section:'risks', thread:null}, 'stage-document-editor')),
      node('span',' · '), link('Все вопросы фичи или этапа', stageURL({risk:null, thread:null}, 'discussion-heading')));
    const existing = view.threads.some(t => t.risk_id === id);
    if (!existing && !query.has('revision')) {
      const question = document.getElementById('ask-subject');
      document.getElementById('new-question').open = true;
      if (!question.value) question.value = 'Риск ' + id;
    }
    if (location.hash === '#discussion-heading') discussion.scrollIntoView({block:'start'});
  }

  editor.innerHTML = `
    <h2 data-doc-title tabindex="-1">Рабочее место</h2>
    <p data-doc-scope class="muted"></p>
    <nav data-doc-sections class="inline" aria-label="Разделы рабочего места"></nav>
    <div class="inline doc-actions"><a data-doc-obsidian hidden>Открыть в Obsidian</a>
      <a data-doc-return>Вернуться к этапу</a></div>
    <p data-doc-status role="status"></p>
    <form data-doc-form hidden>
      <label>Текст Markdown<textarea data-doc-text rows="14" spellcheck="false"></textarea></label>
      <div data-doc-conflict hidden>
        <h3>Документ изменился</h3>
        <p>Сверьте черновик с новой версией источника. Обе версии доступны до сохранения.</p>
        <details><summary>Исходный текст при открытии</summary><pre data-doc-before></pre></details>
        <label>Новый текст в базе<textarea data-doc-latest readonly rows="8"></textarea></label>
        <button type="button" data-doc-rebase>Продолжить со своим текстом на новой основе</button>
        <button type="button" data-doc-discard>Взять текст из базы</button>
      </div>
      <label data-doc-by>Автор изменения<input data-doc-author maxlength="120" autocomplete="name" required></label>
      <button data-doc-save type="submit" class="primary">Сохранить рабочий текст</button>
    </form>
    <p data-doc-receipt></p>
    <section data-doc-impact hidden aria-label="Проверка последствий изменения"></section>`;
  const part = key => editor.querySelector('[data-doc-' + key + ']');
  const routeStorage = key => 'chief-document-draft:' + key;
  function readDraft(key) {
    try { return JSON.parse(sessionStorage.getItem(routeStorage(key))); } catch { return null; }
  }
  function forgetDraft(key) {
    try { sessionStorage.removeItem(routeStorage(key)); } catch { /* Хранилище вкладки может быть недоступно. */ }
  }
  function rememberDraft() {
    if (!documentRoute || !original || original.readonly) return;
    if (part('text').value === original.text && !pendingRequest) {forgetDraft(documentRoute);return;}
    const draft = {hash:original.hash, revision:original.revision, before:original.text, text:part('text').value, author:part('author').value, retry:pendingRequest};
    try { sessionStorage.setItem(routeStorage(documentRoute), JSON.stringify(draft)); }
    catch { part('status').textContent = 'Черновик доступен в открытом редакторе. Хранилищу вкладки не хватает места.'; }
  }
  function showConflict(latest, previous) {
    conflict = latest;
    part('conflict').hidden = false;
    part('before').textContent = previous;
    part('latest').value = latest.text;
    part('save').disabled = true;
    part('status').textContent = 'Сохранение приостановлено. Источник изменился со времени открытия.';
  }
  function showReceipt(receipt) {
    part('receipt').replaceChildren();
    if (!receipt) return;
    const instant = new Date(receipt.saved_at);
    const date = Number.isFinite(instant.getTime()) ? dateFormat.format(instant) : '';
    part('receipt').append(node('strong','Квитанция сохранения'), node('span',' · ' +
      [date,receipt.author,'ред. ' + receipt.revision].filter(Boolean).join(' · ')));
    const details = node('details');
    details.append(node('summary','Номер операции и отпечаток текста'),node('code',receipt.operation_id),node('br'),node('code',receipt.hash));
    part('receipt').append(details);
  }
  function reviewLink(label, href) {
    const item = link(label, href);
    item.target = '_blank';
    item.rel = 'noopener';
    return item;
  }
  function traceReview(trace, presence) {
    const row = node('li');
    row.append(node('strong', trace.invariant), node('span', ' · редакция ' + trace.revision));
    row.append(node('blockquote', trace.rule), node('span', presence));
    const actions = node('div', null, 'inline');
    actions.append(reviewLink('Задача ' + trace.task_id, '/?' + new URLSearchParams({task:trace.task_id, view:'details'})));
    if (trace.test_event_id && trace.test?.log) {
      const args = new URLSearchParams({task:trace.task_id, event:trace.test_event_id});
      actions.append(reviewLink('Исходный журнал проверки', '/api/stage/log?' + args));
    } else {
      actions.append(node('span', trace.test_event_id ? 'Файл журнала отсутствует' : 'Запуск не привязан', 'muted'));
    }
    if (trace.test?.case_name) actions.append(node('span', trace.test.case_name));
    row.append(actions);
    return row;
  }
  function showImpact() {
    const panel = part('impact');
    panel.hidden = !original || !['requirements', 'nfr'].includes(original.section);
    if (panel.hidden) {impactSignature = '';return;}
    const {stage, feature, section, readonly, last_receipt:receipt} = original;
    const source = original.text.replace(/\r\n?/g, '\n');
    const proposed = part('text').value;
    const draft = source !== proposed;
    // Квитанция описывает предыдущий текст только пока её итоговый хэш совпадает с открытым документом.
    const previous = receipt?.hash === original.hash ? receipt.previous_hash : null;
    const traces = (view.traces || []).filter(trace =>
      trace.stage === stage && (trace.feature || stage) === feature && trace.section === section &&
      (!readonly || trace.source_hash === original.hash));
    const groups = [[], []];
    for (const trace of traces) {
      const rule = trace.rule.replace(/\r\n?/g, '\n');
      const contains = proposed.includes(rule);
      const presence = contains ? (draft ? 'Цитата сохранена в черновике.' : 'Цитата есть в открытом тексте.')
        : (draft ? 'В черновике нет этой точной цитаты.' : 'В открытом тексте нет этой точной цитаты.');
      const fingerprint = trace.source_hash;
      const same = fingerprint === original.hash;
      const beforeSave = !same && fingerprint === previous;
      groups[same || beforeSave ? 0 : 1].push({trace, presence, basis:same ? 'Открытый исходник' :
        beforeSave ? 'Исходник перед последним сохранением' : 'Другой снимок раздела'});
    }
    // Опрос панели и ввод в редакторе не должны сбрасывать раскрытый список или фокус ссылки.
    const signature = JSON.stringify([documentRoute, new URLSearchParams(location.search).get('team'), draft, groups.map(group => group.map(row =>
      [row.trace.id, row.basis, row.presence]))]);
    if (signature === impactSignature) return;
    impactSignature = signature;
    panel.replaceChildren(node('h3', 'Какие связи пересмотреть'));
    const addList = (parent, rows) => {
      const list = node('ul', null, 'doc-impact-traces');
      for (const {trace, presence, basis} of rows) list.append(traceReview(trace, basis + '. ' + presence));
      parent.append(list);
    };
    if (groups[0].length) {
      panel.append(node('h4', 'Зарегистрированные задачи и проверки'));
      addList(panel, groups[0]);
    } else {
      panel.append(node('p', 'Для этого текста пока нет зарегистрированных связей с задачами или проверками.'));
    }
    if (groups[1].length) {
      const history = node('details');
      history.append(node('summary', 'Связи с другими снимками раздела · ' + groups[1].length));
      addList(history, groups[1]);
      panel.append(history);
    }
    if ((groups[0].length || groups[1].length) && !readonly) {
      panel.append(node('p', 'Запуск относится к тексту на момент регистрации связи. Для проверки новых условий зарегистрируйте новый результат.', 'muted'));
    }
    const candidates = node('div', null, 'inline');
    for (const key of ['effort', 'risks']) {
      candidates.append(reviewLink(sections[key], stageURL({section:key, risk:null, thread:null, revision:null}, 'stage-document-editor')));
    }
    panel.append(node('h4', 'Кандидаты на пересмотр'), node('p',
      (feature === stage ? 'Рабочие документы этого этапа.' : 'Рабочие документы этой фичи.') +
      ' Команда решает, влияет ли изменение на оценку или резерв.'), candidates);
  }
  function putDocument(data) {
    original = data;
    conflict = null;
    part('form').hidden = false;
    part('text').value = data.text;
    part('text').readOnly = data.readonly;
    part('by').hidden = part('save').hidden = data.readonly;
    part('author').disabled = data.readonly;
    part('save').disabled = data.readonly;
    part('conflict').hidden = true;
    part('scope').textContent = recordName(data.feature) + ' · ' + (data.readonly ? 'Архивная редакция ' : 'Рабочий документ · базовая редакция ') + data.revision;
    const obsidian = obsidianLink(data.obsidian_url);
    part('obsidian').hidden = !obsidian;
    if (obsidian) part('obsidian').href = obsidian.href;
    part('status').textContent = '';
    showReceipt(data.last_receipt);
  }
  async function request(url, init) {
    const response = await fetch(url, init);
    const data = await response.json().catch(()=>null);
    if (!response.ok || !data) {
      const error = new Error(data?.error || 'Не удалось получить ответ сервера (' + response.status + ').');
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function readSource(args) {
    return request('/api/stage/document?' + new URLSearchParams(args));
  }
  async function loadDocument(args, ticket, draft) {
    try {
      const data = await readSource(args);
      if (ticket !== generation) return;
      putDocument(data);
      if (draft && !data.readonly) {
        // Если ответ на запись пропал, повторяем с прежним ID операции.
        const committed = draft.retry && draft.retry.id === data.last_receipt?.operation_id;
        if (committed && data.hash === data.last_receipt.hash) {
          forgetDraft(documentRoute);
        } else {
          part('text').value = draft.text;
          part('author').value = draft.author || '';
          if (draft.hash !== data.hash || draft.revision !== data.revision) {
            original = {...data, hash:draft.hash, revision:draft.revision, text:draft.before};
            showConflict(data, draft.before);
          } else {
            pendingRequest = draft.retry || null;
            part('status').textContent = 'Восстановлен черновик этой вкладки.';
          }
        }
      }
      showImpact();
    } catch (error) {
      if (ticket !== generation) return;
      part('status').replaceChildren(node('span', error.message + ' '));
      const again = node('button','Повторить загрузку');again.type='button';
      again.onclick=()=>loadDocument(args, ticket, draft);
      part('status').append(again);
    }
  }
  function showDocument(query) {
    const section = query.get('section'), feature = query.get('feature') || view.stage.key;
    const valid = Object.hasOwn(sections, section);
    const args = {stage:view.stage.key, feature, section};
    if (query.get('revision')) args.revision = query.get('revision');
    const key = valid ? JSON.stringify([view.project.id, args]) : null;
    const selectedRisk = query.get('risk');
    const back = part('return');
    back.href = stageURL({section:null}, selectedRisk ? 'stage-risks' : 'tabs');
    back.textContent = selectedRisk ? 'Вернуться к риску ' + selectedRisk : 'Вернуться к обзору';
    if (key === documentRoute) {showImpact();return;}
    rememberDraft();
    documentRoute = key; generation++; saving = false;
    original = conflict = pendingRequest = null;
    showImpact();
    editor.hidden = !key;
    if (!key) return;
    part('form').hidden = true;
    part('receipt').replaceChildren();
    part('obsidian').hidden = true;
    part('author').value = '';
    part('scope').textContent = recordName(feature);
    part('title').textContent = sections[section] + ' · Рабочее место';
    part('status').textContent = 'Чтение документа…';
    const target = view.features.find(f=>f.key === feature) || view.stage;
    const available = args.revision ? target.saved_sections?.[args.revision] || [] : Object.keys(sections);
    const nav = part('sections');
    nav.replaceChildren();
    for (const [key, label] of Object.entries(sections)) {
      if (!available.includes(key)) continue;
      const tab = link(label,stageURL({section:key, risk:key === 'risks' ? selectedRisk : null, thread:null},'stage-document-editor'));
      if (key === section) tab.setAttribute('aria-current','page');
      nav.append(tab);
    }
    loadDocument(args, generation, readDraft(key));
    if (!location.hash || location.hash === '#stage-document-editor') part('title').focus();
  }
  function refresh() {
    if (!view?.stage || new URLSearchParams(location.search).get('stage') !== view.stage.key) return;
    const query = new URLSearchParams(location.search);
    showRegister(query); showTopic(query); showDocument(query);
  }
  document.addEventListener('stage-view', event => {view = event.detail; refresh();});
  document.addEventListener('stage-context', refresh);
  part('text').addEventListener('input', () => {rememberDraft();showImpact();});
  part('author').addEventListener('input', rememberDraft);
  window.addEventListener('pagehide', rememberDraft);
  function replaceBasis(keepText) {
    if (!conflict) return;
    const mine = part('text').value;
    putDocument(conflict);
    pendingRequest = null;
    if (keepText) part('text').value = mine;
    part('status').textContent = keepText ? 'Основа обновлена. Проверьте текст и нажмите «Сохранить рабочий текст».' : 'Открыт актуальный текст из базы.';
    rememberDraft(); showImpact(); part('text').focus();
  }
  part('rebase').addEventListener('click',()=>replaceBasis(true));
  part('discard').addEventListener('click',()=>replaceBasis(false));
  part('form').addEventListener('submit',async event=>{
    event.preventDefault();
    if (!original || original.readonly || saving || conflict || !event.target.reportValidity()) return;
    const text = part('text').value, author = part('author').value.trim();
    if (!author) {part('status').textContent='Укажите автора изменения.';return;}
    if (text === original.text) {part('status').textContent='Текст совпадает с рабочей версией.';return;}
    const ticket = generation, args = {stage:original.stage, feature:original.feature, section:original.section};
    const payload = {...args, project_id:view.project.id, expected_revision:original.revision, expected_hash:original.hash, text, author};
    const signature = JSON.stringify(payload);
    if (pendingRequest?.signature !== signature) pendingRequest = {signature,id:crypto.randomUUID()};
    const operation = pendingRequest.id;
    rememberDraft();saving = true;part('save').disabled=true;part('status').textContent='Сохранение…';
    try {
      const {token} = await request('/api/ui');
      const receipt = await request('/api/stage/document', {method:'POST',
        headers:{'Content-Type':'application/json','X-Dashboard-UI-Token':token},body:JSON.stringify({...payload,operation_id:operation})});
      if (ticket !== generation) return;
      original = {...original, hash:receipt.hash, revision:receipt.revision, text, last_receipt:receipt};
      pendingRequest = null;rememberDraft();showReceipt(receipt);showImpact();
      part('status').textContent = part('text').value === text ? 'Рабочий текст сохранён.' : 'Отправленный текст сохранён. В редакторе остались более свежие изменения.';
      document.dispatchEvent(new Event('stage-refresh'));
    } catch (error) {
      if (ticket !== generation) return;
      part('status').textContent = error.message;
      if (error.status === 409) {
        try {
          const latest = await readSource(args);
          if (ticket === generation) showConflict(latest, original.text);
        } catch (failure) {
          if (ticket === generation) part('status').textContent = error.message + ' ' + failure.message;
        }
      }
    } finally {
      if (ticket === generation) {saving = false;part('save').disabled=Boolean(conflict);}
    }
  });
})();
