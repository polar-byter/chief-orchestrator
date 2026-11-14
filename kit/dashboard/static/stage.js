'use strict';

(() => {
  const byId = id => document.getElementById(id);
  const roles = {manager: 'РП', analyst: 'Аналитик', engineer: 'Инженеры'};
  const sections = {requirements: 'Требования', bdd: 'BDD', nfr: 'NFR', effort: 'Оценка трудозатрат', risks: 'Риски'};
  const actions = {ask: 'Вопрос', reply: 'Ответ', reopen: 'Возобновление'};
  const statuses = {passed: 'пройдена', failed: 'ошибка', skipped: 'есть пропуски', error: 'ошибка', ok: 'пройдена'};
  const runScope = scope => ({full:'Полный набор', stage:'Проверка этапа', baseline:'Исходная проверка'}[scope] || scope || 'Запуск');
  const states = {working: 'Рабочий снимок', initial: 'Исходная редакция', accepted: 'Принятая редакция'};
  const parameters = new URLSearchParams(location.search);
  const tabs = Array.from(byId('tabs').querySelectorAll('[role="tab"]'));
  const drafts = new Map();
  const dateFormat = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'short', timeStyle: 'short'});
  const hoursFormat = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 1, minimumFractionDigits: 1});
  let stageKey = parameters.get('stage') || '';
  let featureKey = parameters.get('feature') || '';
  let revision = parameters.get('revision') || '';
  let activeTeam = tabs.some(tab => tab.dataset.team === parameters.get('team')) ? parameters.get('team') : 'manager';
  let current = null;
  let snapshot = '';
  let requestNumber = 0;
  let runsRequest = 0;
  let threadId = parameters.get('thread') || '';
  let traceId = '';
  let threadVersion = null;
  let loading = false;
  let posting = false;
  let taskOptionsLoaded = false;

  function el(tag, value = null, className = '') {
    const node = document.createElement(tag);
    if (value !== null) node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function formatTime(seconds) {
    return Number.isFinite(seconds) ? dateFormat.format(new Date(seconds * 1000)) : 'Время неизвестно';
  }
  function hours(value) {
    return Number.isFinite(value) ? hoursFormat.format(value) + ' чел·ч' : 'Нет данных';
  }
  function featureTitle(key) {
    return current?.features.find(f => f.key === key)?.title || key || 'Весь этап';
  }
  function selectedFeatures() {
    return current.features.filter(f => !featureKey || f.key === featureKey);
  }
  function visibleWork() {
    return featureKey ? selectedFeatures() : [current.stage, ...current.features];
  }
  function scoped(records) {
    if (revision) return records.filter(record => record.feature === featureKey && String(record.revision) === revision);
    return records.filter(record => !featureKey || record.feature === featureKey);
  }
  function currentRecord() {
    return current.features.find(f => f.key === featureKey) || current.stage;
  }
  function safeURL(raw) {
    if (!raw) return '';
    try {
      const url = new URL(raw, location.href);
      if (url.origin === location.origin && ['http:', 'https:'].includes(url.protocol)) return url.href;
    } catch { /* Пустой адрес оставляет видимую подпись источника. */ }
    return '';
  }
  function sourceLink(raw, label) {
    const url = safeURL(raw);
    if (!url) return el('span', label);
    const link = el('a', label);
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener';
    return link;
  }
  function taskURL(task) {
    return '/?' + new URLSearchParams({task, view: 'details'});
  }
  function workspaceLink(record, section, label, saved = '') {
    const query = new URLSearchParams({stage:stageKey, team:activeTeam, section});
    if (current?.project.id) query.set('project', current.project.id);
    if (record !== stageKey) query.set('feature', record);
    if (saved) query.set('revision', saved);
    const link = el('a', label);
    link.href = '/stage?' + query + '#stage-document-editor';
    return link;
  }
  function clearRiskRoute() {
    const url = new URL(location.href);
    url.searchParams.delete('risk');
    url.searchParams.delete('risk_hash');
    url.searchParams.delete('thread');
    history.replaceState(null, '', url);
  }
  function documentLinks(record, names) {
    const links = el('span', null, 'inline');
    for (const name of names) if (record.documents?.[name]) links.append(workspaceLink(record.key, name, sections[name]));
    return links;
  }
  function row(title, ...contents) {
    const box = el('div', null, 'row');
    const cell = el('div');
    cell.append(...contents);
    box.append(el('span', title), cell);
    return box;
  }
  function empty(text) { return el('p', text, 'empty'); }
  function replaceRows(id, rows, message) {
    byId(id).replaceChildren(...(rows.length ? rows : [empty(message)]));
  }
  function metric(label, value) {
    const box = el('div', null, 'metric');
    box.append(el('strong', value), el('span', label));
    return box;
  }
  function options(id, pairs, selected) {
    const select = byId(id);
    if (select.options.length !== pairs.length || pairs.some(([key, label], i) =>
      select.options[i].value !== key || select.options[i].text !== label)) {
      select.replaceChildren(...pairs.map(([key, label]) => new Option(label, key)));
    }
    if (pairs.some(([key]) => key === selected)) select.value = selected;
    return select.value;
  }
  function updateURL() {
    const url = new URL(location.href);
    for (const [key, value] of [['stage',stageKey], ['feature',featureKey], ['revision',revision], ['team',activeTeam]]) {
      if (value) url.searchParams.set(key,value); else url.searchParams.delete(key);
    }
    history.replaceState(null, '', url);
  }
  function showTeam(team) {
    const previous = activeTeam;
    activeTeam = team;
    for (const tab of tabs) {
      const selected = tab.dataset.team === team;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      byId(tab.getAttribute('aria-controls')).hidden = !selected;
    }
    byId('trace-section').hidden = team === 'manager';
    if (previous !== team && !byId('ask-text').value && !byId('ask-subject').value) {
      byId('ask-role').value = team === 'joint' ? 'analyst' : team;
      byId('ask-assignee').value = team === 'manager' ? 'analyst' : 'manager';
    }
    byId('assignments-heading').textContent = team === 'joint' ? 'Поручения команд' : 'Поручения · ' + roles[team];
    if (current?.stage) {
      renderTasks();
      renderDiscussions();
    }
    updateURL();
    document.dispatchEvent(new Event('stage-context'));
  }
  async function json(url, init) {
    const response = await fetch(url, init);
    const value = await response.json().catch(() => null);
    if (!response.ok || value === null) throw new Error(value?.error || value?.message || 'Ошибка запроса: ' + response.status);
    return value;
  }
  async function refresh(force = false) {
    if (loading && !force) return;
    const request = ++requestNumber;
    loading = true;
    try {
      const data = await json('/api/stage?' + new URLSearchParams(stageKey ? {stage:stageKey} : {}));
      if (request !== requestNumber) return;
      const expectedProject = parameters.get('project');
      if (expectedProject && data.project?.id !== expectedProject) {
        current = null;
        snapshot = '';
        for (const id of ['monitor', 'selectors', 'record-documents']) byId(id).hidden = true;
        throw new Error('По этому адресу открыт другой проект. Запустите дашборд нужного проекта и снова откройте ссылку в Obsidian.');
      }
      const content = JSON.stringify(data);
      current = data;
      if (content !== snapshot) {
        snapshot = content;
        renderStage();
      }
      byId('status').textContent = data.stage ? '' : 'В проекте ещё нет этапов. Откройте проектную базу Чифа и добавьте этап.';
      byId('last-update').textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU');
      document.dispatchEvent(new CustomEvent('stage-view', {detail:data}));
    } catch (error) {
      if (request === requestNumber) byId('status').textContent = error.message;
    } finally {
      if (request === requestNumber) loading = false;
    }
  }
  function renderStage() {
    const data = current;
    byId('project-name').textContent = data.project?.name || 'Проект';
    byId('monitor').hidden = !data.stage;
    byId('selectors').hidden = !data.stage;
    byId('record-documents').hidden = !data.stage;
    if (!data.stage) return;
    stageKey = data.stage.key;
    byId('stage-title').textContent = data.stage.title;
    document.title = data.stage.title + ' · Монитор Чифа';
    byId('stage-revision').textContent = 'Этап ' + stageKey + ' · ред. ' + data.stage.revision;
    options('stage-select', data.stages.map(s => [s.key, s.title + ' · ' + s.key]), stageKey);
    const featureOptions = data.features.map(f => [f.key, f.title + ' · ' + f.key]);
    if (!data.features.some(f => f.key === featureKey)) featureKey = '';
    options('feature-select', [['','Все фичи'], ...featureOptions], featureKey);
    const wasFeature = byId('trace-feature').value;
    options('trace-feature', featureKey ? featureOptions.filter(([key]) => key === featureKey) : [['','Этап целиком'], ...featureOptions], featureKey || wasFeature);
    if (wasFeature !== byId('trace-feature').value || !byId('trace-revision').value) traceRevision();
    const target = currentRecord();
    const history = target.revisions || [];
    if (!history.map(String).includes(revision)) revision = '';
    options('record-revision', [['','Рабочие документы'], ...history.map(n=>[String(n),'Снимок · редакция ' + n])],revision);
    const documentURL = section => '/api/stage/source?' + new URLSearchParams({
      stage:stageKey,feature:featureKey || stageKey,section,...(revision ? {revision} : {})});
    const archive = revision ? documentURL('card') : safeURL(target.archive);
    byId('archive-link').href = archive || '';
    byId('archive-link').hidden = !archive;
    const saved = revision && target.saved_sections ? target.saved_sections[revision] || [] : Object.keys(sections);
    byId('record-documents').replaceChildren(...Object.entries(sections).map(([key,label]) =>
      saved.includes(key) ? workspaceLink(target.key, key, label, revision) : el('span',label + ' · нет снимка','muted')));
    byId('revision-hint').textContent = revision ? 'Документы, обсуждения и связи — редакция ' + revision + ' пункта «' + target.title + '». Поручения и прогноз показывают текущую работу.' : '';
    byId('revision-hint').hidden = !revision;
    renderManagement();
    renderAnalysis();
    renderEngineering();
    renderTraces();
    showTeam(activeTeam);
  }
  function renderManagement() {
    const features = selectedFeatures(), complete = features.filter(f => Number.isFinite(f.estimate.total));
    const forecast = current.decisions?.forecast;
    let value = null;
    if (!featureKey && forecast) value = forecast.known_hours;
    else if (featureKey && features.length) value = features[0].estimate.known ?? features[0].estimate.total;
    byId('management-metrics').replaceChildren(metric('Известная часть оценки', hours(value)), metric('Полностью оценено фич', complete.length + ' из ' + features.length));
    const rows = features.map(f => row(f.title, el('p', Number.isFinite(f.estimate.total) ? 'С резервом ' + hours(f.estimate.total) : 'Неполная оценка'), documentLinks(f,['effort','risks'])));
    replaceRows('feature-estimates',rows,'У этапа ещё нет фич.');
  }
  function renderAnalysis() {
    const rows = visibleWork().map(f => {
      const notes = (f.checks || []).map(c => el('p', c.text));
      return row(f.title, ...(notes.length ? notes : [el('p','Нет зарегистрированных замечаний к условиям.')]), documentLinks(f,['requirements','bdd','nfr']));
    });
    replaceRows('analysis-checks',rows,'Выберите этап с документами фич.');
  }
  function renderTasks() {
    const records = visibleWork(), scope = activeTeam === 'joint' ? 'all' : activeTeam;
    const counts = records.map(record => record.progress?.[scope]);
    const countsKnown = counts.length > 0 && counts.every(c =>
      c && [c.open,c.done].every(n => Number.isSafeInteger(n) && n >= 0));
    const open = countsKnown ? counts.reduce((sum,c) => sum + c.open, 0) : 0;
    const done = countsKnown ? counts.reduce((sum,c) => sum + c.done, 0) : 0;
    const total = open + done;
    const progress = byId('assignment-progress');
    progress.hidden = !countsKnown || !total;
    if (!progress.hidden) {progress.max = total; progress.value = done;}
    byId('assignment-counts').textContent = !countsKnown ? 'Количество отмеченных поручений неизвестно.' :
      total ? 'По флажкам в документах: выполнено ' + done + ' из ' + total + ' · открыто ' + open :
      'Открыто 0. Отмеченных поручений пока нет, прогресс неизвестен.';
    const rows = records.flatMap(f => f.tasks.flatMap(task => {
      const owners = task.roles || [task.role];
      if (activeTeam !== 'joint' && !owners.includes(activeTeam)) return [];
      return [row(f.title, el('p', task.text), el('span', owners.map(role => roles[role] || role).join(', ') + ' · '),
        workspaceLink(f.key, task.section, sections[task.section] || task.section))];
    }));
    replaceRows('task-list',rows,'Открытых поручений в выбранном контексте нет.');
  }
  function renderEngineering() {
    const runs = new Map();
    for (const trace of scoped(current.traces)) {
      if (trace.test) runs.set(trace.task_id + '/' + trace.test_event_id,trace);
    }
    const chart = el('div',null,'run-list');
    for (const trace of [...runs.values()].sort((a,b) => a.test.at - b.test.at)) {
      const test = trace.test, link = sourceLink(trace.log_url || taskURL(trace.task_id), '');
      const title = el('span');
      title.append(el('strong',runScope(test.scope)),el('br'),el('small',formatTime(test.at) + ' · ' + trace.task_id));
      const reading = el('span'), bars = el('span',null,'bars');
      const countsKnown = [test.tests,test.failures,test.errors,test.skipped].every(x => Number.isSafeInteger(x) && x >= 0);
      const passed = countsKnown ? test.tests - test.failures - test.errors - test.skipped : null;
      const validCounts = passed !== null && passed >= 0;
      if (validCounts && test.tests) {
        for (const [key,value] of [['passed',passed],['failed',test.failures + test.errors],['skipped',test.skipped]]) {
          const bar = el('span',null,key);
          bar.style.width = (100 * value / test.tests) + '%';
          bars.append(bar);
        }
        reading.append(bars);
      }
      reading.append(el('small',validCounts ? passed + ' прошли · ' + (test.failures + test.errors) + ' ошибок и падений · ' + test.skipped + ' пропущено' : 'Количество проверок неизвестно'));
      link.append(title,reading); chart.append(link);
    }
    const requests = selectedFeatures().filter(f => f.request).map(f => row(
      f.title, sourceLink(taskURL(f.request),'Открытая заявка · ' + f.request)));
    byId('test-history').replaceChildren(chart.children.length ? chart : empty('К инвариантам ещё не привязаны запуски тестов. Сохраните связь с событием из журнала.'), ...requests);
  }
  function traceRevision() {
    const key = byId('trace-feature').value;
    const f = current?.features.find(f => f.key === key) || current?.stage;
    byId('trace-revision').value = (key === featureKey && revision) || f?.revision || '';
    if (revision && key === featureKey) byId('trace-source').value = 'archive';
  }
  function traceSourceURL() {
    const feature = byId('trace-feature').value || stageKey, revision = byId('trace-revision').value;
    if (!feature) return '';
    const args = new URLSearchParams({stage: stageKey, feature, section:byId('trace-section-key').value});
    if (byId('trace-source').value === 'archive') args.set('revision',revision);
    return '/api/stage/source?' + args;
  }
  function renderTraces() {
    const traces = scoped(current.traces);
    traceId = options('trace-select', traces.map(t => [t.id, t.invariant + ' · ' + featureTitle(t.feature) + ' · ред. ' + t.revision]), traceId);
    byId('trace-picker').hidden = !traces.length;
    byId('trace-input-link').href = traceSourceURL();
    byId('trace-input-link').hidden = !traceSourceURL();
    const trace = traces.find(t => t.id === traceId);
    const view = byId('trace-view');
    if (!trace) {
      view.replaceChildren(empty('В выбранном контексте ещё нет связей инвариантов с задачами.'));
      return;
    }
    const chain = el('ol',null,'chain');
    function step(title, ...contents) {
      const item = el('li');
      item.append(el('small',title),...contents); chain.append(item);
    }
    step('Базовая редакция',sourceLink(trace.card_url,featureTitle(trace.feature) + ' · ' + trace.revision));
    step('Инвариант',el('b',trace.invariant),el('small',states[trace.source_state] || (trace.source === 'working' ? 'Рабочий снимок' : 'Архив')),sourceLink(trace.source === 'archive' ? trace.archive_url : trace.working_url, sections[trace.section] || trace.section));
    step('Задача',sourceLink(taskURL(trace.task_id),trace.task_id));
    if (trace.test) {
      const test = trace.test;
      step('Запуск',sourceLink(trace.log_url || taskURL(trace.task_id),runScope(test.scope) + ' · ' + (statuses[test.status] || test.status)),el('small',formatTime(test.at)));
    } else step('Запуск',el('span','Проверка не связана'));
    view.replaceChildren(chain,el('blockquote',trace.rule));
    if (trace.case_name || trace.test?.case_name) view.append(el('p','Отдельный тест · ' + (trace.case_name || trace.test.case_name) + ' · ' + (trace.test?.case_status || 'нет результата')));
    if (trace.source_hash) {
      const info = el('details');
      info.append(el('summary','Отпечаток исходника'),el('code',trace.source_hash)); view.append(info);
    }
  }
  function renderDiscussions() {
    const risk = discussionRisk();
    const threads = scoped(current.threads).filter(t => !risk || t.risk_id === risk);
    const handoffs = threads.filter(t => t.status === 'open').map(t => {
      const open = el('button',t.subject || 'Открыть вопрос');
      open.type = 'button';
      open.addEventListener('click',()=>{selectThread(t.id);byId('thread-select').focus();});
      return row(featureTitle(t.feature),open,el('p','Ожидается ответ · ' + (roles[t.assignee] || t.assignee) + (t.blocking ? ' · блокирует решение' : '')));
    });
    replaceRows('handoffs',handoffs,'Открытых вопросов между командами нет.');
    const before = threadId;
    threadId = options('thread-select',threads.map(t => [t.id,(t.status === 'answered' ? 'Есть ответ' : 'Открыт') + ' · ' + (t.subject || 'Вопрос') + ' · ' + featureTitle(t.feature)]),threadId);
    if (threadId !== before) threadVersion = null;
    byId('thread-detail').hidden = !threadId;
    byId('threads-empty').textContent = threads.length ? '' : risk ? 'По этому риску вопросов пока нет.' : 'По выбранному этапу или фиче вопросов пока нет.';
    const t = threads.find(t => t.id === threadId);
    if (!t) return;
    const list = el('ol',null,'messages');
    for (const m of t.messages) {
      const item = el('li');
      item.append(el('strong',(roles[m.role] || m.role) + (m.author ? ' · ' + m.author : '')),
        el('small',' → ' + (roles[m.assignee] || m.assignee) + ' · ' + (actions[m.action] || m.action) + ' · ' + formatTime(m.at)),
        el('p',m.text));
      list.append(item);
    }
    byId('thread-view').replaceChildren(el('p',featureTitle(t.feature) + ' · ред. ' + t.revision +
      (t.risk_id ? ' · риск ' + t.risk_id : '') + (t.blocking ? ' · блокирующий вопрос' : '')),list);
    if (t.has_risk_snapshot) {
      const query = new URLSearchParams({stage:stageKey, thread:t.id});
      byId('thread-view').append(sourceLink('/api/stage/discussion-source?' + query, 'Документ риска на момент вопроса'));
    }
    if (threadVersion === null || !byId('reply-text').value.trim()) threadVersion = t.version;
    const hasAnswer = t.status === 'answered';
    byId('reply-label').textContent = hasAnswer ? 'Причина повторного обсуждения' : 'Ответ';
    byId('reply-submit').textContent = hasAnswer ? 'Вернуть на обсуждение' : 'Ответить';
    if (!byId('reply-text').value.trim()) {
      byId('reply-role').value = activeTeam === 'joint' ? t.assignee : activeTeam;
      byId('reply-assignee').value = t.messages.at(-1)?.role || 'manager';
    }
  }
  function selectThread(id) {
    if (threadId !== id) {saveDraft();threadId = id;threadVersion = null;restoreDraft();}
    renderDiscussions();
  }
  function discussionRisk() {
    return activeTeam === 'joint' ? new URLSearchParams(location.search).get('risk') || '' : '';
  }
  async function listTasks() {
    if (taskOptionsLoaded) return;
    try {
      const tasks = await json('/api/tasks');
      options('trace-task',[['','Выберите задачу'],...tasks.map(t => [t.task,t.title + ' · ' + t.task])],byId('trace-task').value);
      taskOptionsLoaded = true;
    } catch (error) {byId('trace-status').textContent=error.message;}
  }
  async function listRuns() {
    const task = byId('trace-task').value, ticket = ++runsRequest;
    options('trace-run',[['','Пока не связан']],'');
    byId('trace-case').value=''; byId('trace-case').disabled=true;
    if (!task) return;
    try {
      const data = await json('/api/stage/runs?' + new URLSearchParams({task}));
      if (ticket !== runsRequest) return;
      options('trace-run',[['','Пока не связан'],...data.runs.map(run=>[run.event_id,formatTime(run.at) + ' · ' + runScope(run.scope) + ' · ' + (statuses[run.status] || run.status)])],'');
    } catch (error) {if(ticket === runsRequest) byId('trace-status').textContent=error.message;}
  }
  const operationIds = new Map();
  async function submit(form, body, statusId, onSuccess) {
    if (posting || !current?.stage || !form.reportValidity()) return;
    const context = {stage:stageKey,project_id:current.project.id,...body};
    const selected = [featureKey,revision,threadId];
    const signature = JSON.stringify(context);
    if (!operationIds.has(signature)) operationIds.set(signature,crypto.randomUUID());
    posting = true;
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    byId(statusId).textContent = 'Сохранение…';
    try {
      const ui = await json('/api/ui');
      const result = await json('/api/stage/action',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-UI-Token':ui.token},body:JSON.stringify({...context,operation_id:operationIds.get(signature)})});
      operationIds.delete(signature);
      if (context.stage === stageKey && context.project_id === current?.project.id &&
          selected[0] === featureKey && selected[1] === revision &&
          (form.id !== 'message-form' || selected[2] === threadId)) {
        onSuccess(result); byId(statusId).textContent = 'Сохранено';
      }
      await refresh(true);
    } catch (error) {
      byId(statusId).textContent = error.message;
      await refresh(true);
    } finally {posting=false;button.disabled=false;}
  }
  function formValues(prefix,names) {
    return Object.fromEntries(names.map(name=>[name,byId(prefix+'-'+name).value.trim()]));
  }
  function saveDraft() {
    drafts.set(JSON.stringify([stageKey,featureKey,revision,threadId]), {text:byId('reply-text').value,role:byId('reply-role').value,assignee:byId('reply-assignee').value,author:byId('reply-author').value,version:threadVersion});
  }
  function restoreDraft() {
    const draft=drafts.get(JSON.stringify([stageKey,featureKey,revision,threadId]));
    for(const name of ['text','author']) byId('reply-'+name).value = draft?.[name] || '';
    if(draft) for(const name of ['role','assignee']) byId('reply-'+name).value=draft[name];
    threadVersion=draft?.version ?? null;
    byId('reply-status').textContent='';
  }
  function resetForms() {
    byId('ask-form').reset(); byId('trace-form').reset();
    byId('ask-role').value = activeTeam === 'joint' ? 'analyst' : activeTeam;
    byId('ask-assignee').value = activeTeam === 'manager' ? 'analyst' : 'manager';
    byId('trace-role').value = 'engineer';
    byId('trace-status').textContent='';byId('ask-status').textContent='';
    byId('trace-case').disabled=true;
    options('trace-run',[['','Пока не связан']],'');
    ++runsRequest;
  }
  byId('ask-form').addEventListener('submit',event=>{
    event.preventDefault();
    const data=formValues('ask',['role','author','assignee','subject','text']);
    const risk = discussionRisk();
    const riskHash = new URLSearchParams(location.search).get('risk_hash');
    submit(event.target,{action:'ask',feature:featureKey,revision:Number(revision || currentRecord().revision),
      blocking:byId('ask-blocking').checked,...(risk ? {risk_id:risk, risk_source:revision ? 'archive' : 'working'} : {}),
      ...(risk && riskHash ? {risk_hash:riskHash} : {}),...data},'ask-status',result=>{
      byId('ask-text').value='';byId('ask-subject').value='';byId('ask-blocking').checked=false;
      if(result.id) {saveDraft();threadId=result.id;restoreDraft();}
    });
  });
  byId('message-form').addEventListener('submit',event=>{
    event.preventDefault();
    const t=current.threads.find(t=>t.id===threadId);
    if(!t) return;
    if(threadVersion !== t.version) {
      byId('reply-status').textContent='Обсуждение обновилось. Прочтите последние сообщения и повторите отправку.';
      threadVersion=t.version;return;
    }
    submit(event.target,{action:t.status==='answered'?'reopen':'reply',thread_id:t.id,expected_version:threadVersion,
      ...formValues('reply',['role','author','assignee','text'])},'reply-status',()=>{
      byId('reply-text').value='';threadVersion=null;saveDraft();
    });
  });
  byId('trace-form').addEventListener('submit',event=>{
    event.preventDefault();
    submit(event.target,{action:'trace',...formValues('trace',['feature','invariant','source','rule','role','author']),
      revision:Number(byId('trace-revision').value),section:byId('trace-section-key').value,task_id:byId('trace-task').value,
      test_event_id:byId('trace-run').value,case_name:byId('trace-case').value.trim()},'trace-status',result=>{
        if(result.id) traceId=result.id;
    });
  });
  for (const select of document.querySelectorAll('.role-options')) {
    for(const [key,label] of Object.entries(roles)) select.add(new Option(label,key));
  }
  resetForms();
  tabs.forEach((tab,i)=>{
    tab.addEventListener('click',()=>showTeam(tab.dataset.team));
    tab.addEventListener('keydown',event=>{
      const index={Home:0,End:tabs.length-1,ArrowLeft:(i+tabs.length-1)%tabs.length,ArrowRight:(i+1)%tabs.length}[event.key];
      if(index===undefined) return;
      event.preventDefault(); tabs[index].focus();showTeam(tabs[index].dataset.team);
    });
  });
  byId('stage-select').addEventListener('change',()=>{
    clearRiskRoute();
    saveDraft();stageKey=byId('stage-select').value;featureKey='';revision='';threadId='';traceId='';restoreDraft();resetForms();
    current=null;snapshot='';byId('monitor').hidden=true;byId('status').textContent='Загружаем этап…';updateURL();refresh(true);
  });
  byId('feature-select').addEventListener('change',()=>{
    clearRiskRoute();
    saveDraft();featureKey=byId('feature-select').value;revision='';threadId='';traceId='';restoreDraft();resetForms();renderStage();
  });
  byId('record-revision').addEventListener('change',()=>{
    saveDraft();revision=byId('record-revision').value;threadId='';traceId='';restoreDraft();resetForms();traceRevision();renderStage();
  });
  byId('thread-select').addEventListener('change',()=>selectThread(byId('thread-select').value));
  byId('trace-select').addEventListener('change',()=>{traceId=byId('trace-select').value;renderTraces();});
  byId('new-trace').addEventListener('toggle',()=>{if(byId('new-trace').open) listTasks();});
  byId('trace-feature').addEventListener('change',()=>{traceRevision();renderTraces();});
  byId('trace-source').addEventListener('change',renderTraces);
  byId('trace-section-key').addEventListener('change',renderTraces);
  byId('trace-revision').addEventListener('input',renderTraces);
  byId('trace-task').addEventListener('change',listRuns);
  byId('trace-run').addEventListener('change',()=>{
    byId('trace-case').disabled=!byId('trace-run').value;
    if(byId('trace-case').disabled) byId('trace-case').value='';
  });
  document.addEventListener('stage-refresh',()=>refresh(true));
  refresh();
  setInterval(()=>{if(!document.hidden && !posting) refresh();},4000);
})();
