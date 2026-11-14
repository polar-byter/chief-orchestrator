'use strict';

(() => {
  const panel = document.getElementById('stage-resources');
  const ruNumber = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const ruDate = new Intl.DateTimeFormat('ru-RU', {timeZone:'UTC', day:'2-digit', month:'2-digit', year:'numeric'});
  const weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const actionNames = {plan:'Ресурсный план', actual:'Факт', checkpoint:'Снимок прогноза'};
  const unknown = 'Нет данных';
  const fieldSets = {
    teams: [
      ['role','Команда','role'], ['people','Человек','integer'], ['hours_per_day','Часов в день','number'],
      ['allocation','Доля участия, 0–1','share'], ['focus','Фокус-фактор, 0–1','share'], ['rate','Ставка за чел·ч','number']
    ],
    assignments: [['work','Работа или резерв','work'],['role','Ответственная команда','role']],
    dependencies: [['work','Работа','work'],['after','После работы','work'],['wait_days','Дней ожидания','integer']],
    api: [
      ['model','Модель','text'], ['input_tokens','Входные токены','integer'], ['output_tokens','Выходные токены','integer'],
      ['input_rate','Тариф за 1 млн на входе','number'], ['output_rate','Тариф за 1 млн на выходе','number']
    ]
  };
  const headings = {teams:'Состав команд и ставки', assignments:'Назначения и резерв', dependencies:'Зависимости работ', api:'Расходы на API'};
  const addLabels = {teams:'Добавить команду',assignments:'Назначить работу',dependencies:'Добавить зависимость',api:'Добавить модель'};

  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value != null) element.textContent = value;
    if (className) element.className = className;
    return element;
  }
  function dateLabel(iso) {
    if (!iso) return unknown;
    const date = new Date(iso + 'T00:00:00Z');
    return Number.isFinite(date.getTime()) ? ruDate.format(date) : unknown;
  }
  function hours(value) {
    return Number.isFinite(value) ? ruNumber.format(value) + ' чел·ч' : unknown;
  }
  function money(value, currency) {
    return Number.isFinite(value) && currency ? ruNumber.format(value) + ' ' + currency : unknown;
  }
  function percent(value) {
    return Number.isFinite(value) ? ruNumber.format(value) + ' %' : unknown;
  }
  function localToday() {
    const date = new Date();
    return [date.getFullYear(), String(date.getMonth()+1).padStart(2,'0'), String(date.getDate()).padStart(2,'0')].join('-');
  }
  function field(name, title, type='text') {
    const label = node('label', title);
    const input = node('input');
    input.name = name;
    if (type === 'role' || type === 'work') input.setAttribute('list', 'resource-' + type + '-options');
    if (['number','integer','share'].includes(type)) {
      input.type = 'number';
      input.step = type === 'integer' ? '1' : 'any';
      input.min = '0';
      if (type === 'share') input.max = '1';
    } else input.type = type === 'date' ? 'date' : 'text';
    label.append(input);
    return label;
  }
  function table(headings, rows, caption) {
    const wrapper = node('div', null, 'res-table-wrap');
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role','region');
    wrapper.setAttribute('aria-label',caption);
    const grid = node('table');
    grid.append(node('caption',caption,'res-visually-hidden'));
    const head = grid.createTHead().insertRow();
    for (const heading of headings) {
      const th = node('th', heading);
      th.scope = 'col';
      head.append(th);
    }
    const body = grid.createTBody();
    for (const values of rows) {
      const row = body.insertRow();
      values.forEach((value, index) => {
        const cell = node(index === 0 ? 'th' : 'td');
        if (index === 0) cell.scope = 'row';
        cell.append(value);
        row.append(cell);
      });
    }
    wrapper.append(grid);
    return wrapper;
  }
  panel.innerHTML = `
    <div class="res-heading"><h2>Ресурсы всего этапа</h2><span data-coverage></span></div>
    <div data-summary class="res-summary"></div>
    <details data-cost-breakdown><summary>Из чего складывается прогноз стоимости</summary><div data-cost-values></div></details>
    <h3>Загрузка команд</h3>
    <p data-period class="muted"></p><div data-load></div>
    <details data-issues hidden><summary>Что уточнить для расчёта</summary><ul></ul></details>
    <div class="res-editors"></div>
    <details data-facts hidden><summary>Первичные записи факта</summary><div data-facts-table></div></details>
    <h3>План и факт по датам</h3>
    <p data-empty-history class="muted"></p>
    <div data-timeline hidden>
      <div class="res-chart-fields">
        <label>Показатель<select data-indicator>
          <option value="hours">Труд к дате, чел·ч</option><option value="effort">Оценка всего этапа, чел·ч</option>
          <option value="cost">Бюджет этапа и расходы</option><option value="finish">Дата окончания этапа</option>
        </select></label>
        <label data-unit-field hidden>Валюта графика<select data-chart-currency></select></label>
      </div>
      <figure class="res-figure"><div data-legend class="res-legend"></div>
        <canvas data-chart aria-hidden="true"></canvas><figcaption data-graph-note class="muted"></figcaption></figure>
      <details data-points><summary>Значения и источники по датам</summary><div data-history-table></div></details>
    </div>
    <datalist id="resource-role-options"></datalist><datalist id="resource-work-options"></datalist>
  `;
  const part = key => panel.querySelector('[data-' + key + ']');
  let context, lastDisplay = '', generation = 0, indicatorChosen = false;
  const editors = new Map();

  function numberMetric(title, value, detail) {
    const cell = node('div',null,'res-metric');
    cell.append(node('div',title,'res-measure'),node('strong',value));
    if (detail) cell.append(node('small',detail));
    return cell;
  }
  function showSummary(view) {
    const result = view.resources.current;
    const forecast = view.decisions.forecast;
    const baseline = view.resources.baseline;
    part('coverage').textContent = 'Полностью оценено фич: ' + forecast.complete_count + ' из ' + forecast.feature_count;
    part('summary').replaceChildren(
      numberMetric('Оценка этапа',hours(result.total_hours), 'Известная часть · ' + hours(result.known_hours)),
      numberMetric('Прогноз окончания',dateLabel(result.finish), 'Целевая дата · ' + dateLabel(view.resources.plan?.target)),
      numberMetric('Прогноз стоимости',money(result.cost,result.currency), 'Известная часть · ' + money(result.known_cost,result.currency)),
      numberMetric('Зарегистрированный труд',hours(result.actual_hours), result.actual_hours == null && result.known_actual_hours != null ? 'Известная часть · ' + hours(result.known_actual_hours) : ''),
      numberMetric('Зарегистрированные расходы',money(result.actual_cost,result.actual_currency), result.actual_cost == null && result.known_actual_cost != null ? 'Известная часть · ' + money(result.known_actual_cost,result.actual_currency) : ''),
      numberMetric('Бюджет ресурсного плана',money(baseline?.cost,baseline?.currency), baseline?.finish ? 'Окончание по плану · ' + dateLabel(baseline.finish) : '')
    );
    part('cost-values').replaceChildren(table(['Статья','По текущему прогнозу'],[
      ['Труд команд',money(result.planned_labor_cost,result.currency)],
      ['API по моделям',money(result.api_cost,result.currency)],
      ['Прочие расходы',money(result.extra_cost,result.currency)]
    ],'Состав текущего прогноза стоимости'));
    const plan = view.resources.plan || {};
    part('period').textContent = plan.start && plan.target ?
      'Окно загрузки: ' + dateLabel(plan.start) + ' — ' + dateLabel(plan.target) :
      'Для загрузки в процентах укажите начало и целевую дату в ресурсном плане.';
    const rows = (result.teams || []).map(team => {
      const partialLoad = team.load_percent == null && Number.isFinite(team.known_load_percent);
      const loadValue = partialLoad ? team.known_load_percent : team.load_percent;
      const load = node('span',(partialLoad ? 'Известно ' : '') + percent(loadValue), loadValue > 100 ? 'res-overload' : '');
      const demand = team.demand_hours == null && Number.isFinite(team.known_demand_hours) ?
        'Известно ' + hours(team.known_demand_hours) : hours(team.demand_hours);
      const actual = team.actual_hours == null && Number.isFinite(team.known_actual_hours) ?
        'Известно ' + hours(team.known_actual_hours) : hours(team.actual_hours);
      return [team.role || 'Без назначения', demand, hours(team.daily_hours), hours(team.capacity_hours), load, actual];
    });
    part('load').replaceChildren(rows.length ? table(['Команда','Потребность','В рабочий день','Ёмкость за окно','Загрузка','Факт труда'],rows,'Загрузка команд за выбранное окно') : node('p','Сведения о командах ещё не внесены.','muted'));
    const issues = [...new Set([...(forecast.issues || []),...(result.issues || [])])];
    part('issues').hidden = !issues.length;
    part('issues').querySelector('ul').replaceChildren(...issues.map(issue => node('li',issue)));
    const actuals = view.resources.actuals || [];
    part('facts').hidden = !actuals.length;
    if (actuals.length) {
      const rawNumber = value => value == null || value === '' ? null : Number(String(value).replace(',','.'));
      const facts = actuals.slice().sort((a,b)=>b.date.localeCompare(a.date)).map(entry=>[
        dateLabel(entry.date),entry.role,hours(rawNumber(entry.hours)),money(rawNumber(entry.cost),entry.currency),entry.source
      ]);
      part('facts-table').replaceChildren(table(['Дата учёта','Команда','Труд за запись','Расход за запись','Источник'],facts,'Первичные записи факта в исходных единицах'));
    }
  }

  function timelineRows() {
    return context.resources.history || [];
  }
  function graphSeries(key, currency) {
    const metric = (item, name) => item.metrics?.[name];
    const dateValue = iso => iso ? Date.parse(iso+'T00:00:00Z') / 86400000 : null;
    if (key === 'hours') return [
      ['По плану к дате',item=>metric(item,'planned_hours_to_date')],
      ['Зарегистрировано',item=>metric(item,'actual_hours')]
    ];
    if (key === 'effort') return [
      ['По ресурсному плану',item=>item.baseline?.total_hours],['Текущая оценка',item=>metric(item,'total_hours')],
      ['Известная часть',item=>metric(item,'known_hours')]
    ];
    if (key === 'finish') return [
      ['По ресурсному плану',item=>dateValue(item.baseline?.finish)],
      ['Текущий прогноз',item=>dateValue(metric(item,'finish'))]
    ];
    const amount = (metrics, name, unit) => currency && metrics?.[unit] === currency ? metrics[name] : null;
    return [
      ['Бюджет ресурсного плана',item=>amount(item.baseline,'cost','currency')],
      ['Прогноз полной стоимости',item=>amount(item.metrics,'cost','currency')],
      ['Зарегистрированные расходы',item=>amount(item.metrics,'actual_cost','actual_currency')]
    ];
  }
  function drawChart() {
    if (!context?.resources || part('timeline').hidden) return;
    const chart = part('chart'), key = part('indicator').value, currency = part('chart-currency').value;
    const series = graphSeries(key,currency);
    const palette = ['#94caff','#a9dfc1','#f2bb82'];
    part('legend').replaceChildren(...series.map(([title],index) => {
      const item = node('span',title);
      item.style.setProperty('--series-color',palette[index]);
      item.dataset.series = String(index);
      return item;
    }));
    const entries = timelineRows();
    const values = entries.map(entry=>series.map(([,value])=>value(entry)));
    const available = values.flat().filter(Number.isFinite);
    chart.hidden = !available.length;
    part('graph-note').textContent = available.length ? 'Каждая точка — запись. По горизонтали записи идут по дате учёта, внутри дня — по порядку публикации. Разрыв означает отсутствие значения.' : 'Для выбранного показателя пока нет числовых точек.';
    if (!available.length || chart.clientWidth < 100) return;
    const width = chart.clientWidth, height = chart.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    chart.width = Math.round(width * ratio);
    chart.height = Math.round(height * ratio);
    const pen = chart.getContext('2d');
    pen.scale(ratio,ratio);
    pen.font = '12px "Segoe UI", sans-serif';
    let min = key === 'finish' ? Math.min(...available) : 0, max = Math.max(...available);
    if (min === max) max += 1;
    const valueLabel = v => key === 'finish' ? ruDate.format(new Date(Math.round(v) * 86400000)) : ruNumber.format(v);
    const left = Math.ceil(Math.max(pen.measureText(valueLabel(max)).width, pen.measureText(valueLabel(min)).width)) + 12;
    const right = width - 6, top = 10, bottom = height - 28;
    const x = i => entries.length === 1 ? (left+right)/2 : left + i / (entries.length-1) * (right-left);
    const y = value => bottom - (value-min) / (max-min) * (bottom-top);
    pen.lineWidth = 1;
    pen.textAlign = 'right';
    for (const value of [max,min]) {
      pen.strokeStyle = '#46566a'; pen.fillStyle = '#b8c7d7';
      pen.beginPath(); pen.moveTo(left,y(value)); pen.lineTo(right,y(value)); pen.stroke();
      pen.fillText(valueLabel(value), left-8, y(value)+4);
    }
    const tick = entry => dateLabel(entry.date) + ' · №' + entry.version;
    pen.textAlign = 'left'; pen.fillText(tick(entries[0]),left,height-4);
    if (entries.length > 1) { pen.textAlign = 'right'; pen.fillText(tick(entries.at(-1)),right,height-4); }
    series.forEach((_,index) => {
      pen.strokeStyle = pen.fillStyle = palette[index]; pen.lineWidth = 2;
      pen.setLineDash([[],[6,4],[2,3]][index]);
      pen.beginPath();
      let previous = false;
      values.forEach((row,i)=>{
        const value = row[index];
        if (Number.isFinite(value)) {
          if (previous) pen.lineTo(x(i),y(value)); else pen.moveTo(x(i),y(value));
        }
        previous = Number.isFinite(value);
      });
      pen.stroke(); pen.setLineDash([]);
      values.forEach((row,i)=>{
        if (!Number.isFinite(row[index])) return;
        pen.beginPath(); pen.arc(x(i),y(row[index]),3,0,Math.PI*2); pen.fill();
      });
    });
  }
  function historyTable() {
    const rows = timelineRows().slice().reverse().map(item=>{
      const m = item.metrics, b = item.baseline;
      const label = node('span',dateLabel(item.date));
      label.append(node('small', (actionNames[item.action] || item.action) + ' · № ' + item.version));
      const source = node('span',item.source || 'Без источника');
      const created = item.created_at ? new Date(item.created_at) : null;
      const recorded = created && Number.isFinite(created.getTime()) ? created.toLocaleString('ru-RU') : '';
      source.append(node('small',[item.author, recorded].filter(Boolean).join(' · ')));
      const inputs = node('a','Исходные данные');
      inputs.href='/api/stage/resource?' + new URLSearchParams({stage:context.stage.key,version:item.version});
      inputs.target='_blank';inputs.rel='noopener';
      source.append(inputs);
      return [label,hours(m.planned_hours_to_date),hours(m.actual_hours),hours(m.total_hours),hours(m.known_hours),
        dateLabel(m.finish),money(b?.cost,b?.currency),money(m.cost,m.currency),money(m.actual_cost,m.actual_currency),source];
    });
    part('history-table').replaceChildren(table(['Дата учёта','План труда к дате','Факт труда к дате','Оценка этапа','Известный труд','Прогноз срока','Бюджет','Прогноз стоимости','Факт расходов','Основание, автор и время записи'],rows,'История ресурсоёмкости, срока и стоимости'));
  }
  function showHistory() {
    const rows = timelineRows();
    if (!indicatorChosen && rows.length) {
      const hasDatedWork = rows.some(row => Number.isFinite(row.metrics.planned_hours_to_date) || Number.isFinite(row.metrics.actual_hours));
      part('indicator').value = hasDatedWork ? 'hours' : 'effort';
    }
    part('empty-history').hidden = !!rows.length;
    part('empty-history').textContent = 'История появится после первого плана, факта или снимка прогноза.';
    part('timeline').hidden = !rows.length;
    const select = part('chart-currency'), previous = select.value;
    const units = new Set(rows.flatMap(row => [row.baseline?.currency,row.metrics.currency,row.metrics.actual_currency]).filter(Boolean));
    select.replaceChildren(...[...units].sort().map(unit=>new Option(unit,unit)));
    if (units.has(previous)) select.value = previous;
    part('unit-field').hidden = part('indicator').value !== 'cost' || !units.size;
    if (rows.length) {drawChart();historyTable();}
  }
  part('indicator').addEventListener('change',()=>{indicatorChosen=true;showHistory();});
  part('chart-currency').addEventListener('change',drawChart);
  new ResizeObserver(drawChart).observe(part('chart').parentElement);

  function listBlock(key) {
    const section = node('fieldset',null,'res-list');
    section.append(node('legend',headings[key]));
    const rows = node('div',null,'res-rows');
    rows.dataset.rows = key;
    const add = node('button',addLabels[key]);
    add.type = 'button'; add.dataset.add = key;
    add.addEventListener('click',()=>appendRow(rows,key,{}).querySelector('input').focus());
    section.append(rows,add);
    return section;
  }
  function appendRow(container,key,values) {
    const row = node('div',null,'res-edit-row');
    for (const [name,title,type] of fieldSets[key]) {
      const label = field(name,title,type);
      label.querySelector('input').value = values[name] ?? '';
      row.append(label);
    }
    const remove = node('button','Удалить строку');
    remove.type = 'button';
    remove.addEventListener('click',()=>{
      const next = row.nextElementSibling?.querySelector('input') || container.nextElementSibling;
      row.remove();next.focus();
    });
    row.append(remove);container.append(row);
    return row;
  }
  function listValues(form,key) {
    return Array.from(form.querySelector('[data-rows="'+key+'"]').children).map(row=>{
      const result = {};
      for (const [name,,type] of fieldSets[key]) {
        const value = row.querySelector('[name="'+name+'"]').value.trim();
        result[name] = value === '' ? null : type === 'integer' && name === 'wait_days' ? Number(value) : value;
      }
      return result;
    }).filter(row=>Object.values(row).some(value=>value != null));
  }
  function fillPlan(form,plan) {
    for (const key of ['start','target','currency','extra_cost','source']) form.elements[key].value = plan[key] ?? '';
    form.elements.holidays.value = (plan.holidays || []).join(', ');
    const days = Array.isArray(plan.weekdays) ? plan.weekdays : [];
    form.querySelectorAll('[name=weekday]').forEach(box=>box.checked=days.includes(Number(box.value)));
    for (const key of Object.keys(fieldSets)) {
      const container = form.querySelector('[data-rows="'+key+'"]');
      container.replaceChildren();
      for (const row of plan[key] || []) appendRow(container,key,row);
    }
  }
  function workCalendar() {
    const group = node('fieldset',null,'res-weekdays');
    group.append(node('legend','Рабочие дни недели'));
    weekdays.forEach((day,i)=>{
      const label = node('label',null,'flag'), box = node('input');
      box.type = 'checkbox'; box.name = 'weekday'; box.value = i+1;
      label.append(box,day); group.append(label);
    });
    return group;
  }
  function fields(...items) {
    const group = node('div',null,'fields');
    group.append(...items); return group;
  }
  function planFields(form) {
    form.append(fields(field('start','Начало этапа','date'),field('target','Целевая дата','date')),workCalendar(),
      field('holidays','Нерабочие даты (ГГГГ-ММ-ДД через запятую)'),
      fields(field('currency','Валюта плана (например, RUB)'),field('extra_cost','Прочие расходы в валюте плана','number')),
      node('p','Укажите 0, если прочих расходов нет. Пустая сумма оставляет стоимость открытой.','muted'),
      listBlock('teams'),listBlock('assignments'),listBlock('dependencies'),listBlock('api'));
  }
  function planData(form) {
    const values = form.elements;
    const result = {start:values.start.value,target:values.target.value,currency:values.currency.value.trim().toUpperCase(),
      extra_cost:values.extra_cost.value || null,source:values.source.value.trim(),
      weekdays:Array.from(form.querySelectorAll('[name=weekday]:checked'),box=>Number(box.value)),
      holidays:values.holidays.value.trim().split(/[,;\s]+/).filter(Boolean)};
    for (const key of Object.keys(fieldSets)) result[key] = listValues(form,key);
    return result;
  }
  function resourceBasis() {
    return {version:context.resources.version, fingerprint:context.resources.fingerprint || context.decisions.forecast.fingerprint};
  }
  function buildEditor(action,title,submitText) {
    const details = node('details');
    details.dataset.resourceEditor = action;
    const summary = node('summary',title);
    const form = node('form');
    form.dataset.resourceForm = action;
    form.append(fields(field('date',action === 'actual' ? 'Дата учёта' : 'Дата среза','date'),field('author','Автор записи')));
    if (action === 'plan') planFields(form);
    if (action === 'actual') {
      form.append(field('role','Команда','role'),fields(field('hours','Труд за запись, чел·ч','number'),
        field('cost','Расход за запись','number'),field('currency','Валюта расхода')),
        node('p','Введите добавившиеся часы или расходы за указанную дату. Повторное внесение той же работы увеличит факт.','muted'));
    }
    const basis = node('label',action === 'actual' ? 'Основание учёта' : 'Основание');
    const source = node('textarea'); source.name='source';source.required=true;source.rows=2;
    basis.append(source);form.append(basis);
    const status = node('p',null,'form-status');
    status.dataset.resourceStatus = '';status.setAttribute('role','status');
    const buttons = node('div',null,'inline');
    const submit = node('button',submitText,'primary'); submit.type='submit';
    const refresh = node('button','Обновить основу записи'); refresh.type='button'; refresh.hidden=true;
    buttons.append(submit,refresh);
    form.append(buttons,status);details.append(summary,form);
    panel.querySelector('.res-editors').append(details);
    form.elements.date.required = form.elements.author.required = true;
    form.elements.author.maxLength = 100;
    if (action === 'actual') form.elements.role.required = true;
    const editor = {action,details,summary,form,status,submit,refresh,basis:null,retry:null,busy:false};
    editors.set(action,editor);
    function prepare() {
      if (!context || editor.basis) return;
      editor.basis=resourceBasis();
      if (action === 'plan') fillPlan(form,context.resources.plan || {});
      if (action === 'actual') form.elements.currency.value=context.resources.plan?.currency || '';
      form.elements.date.max=context.resources.as_of || localToday();
      form.elements.date.value=form.elements.date.max;
    }
    details.addEventListener('toggle',()=>{if (details.open) prepare();});
    refresh.addEventListener('click',()=>{
      editor.basis=resourceBasis();editor.retry=null;refresh.hidden=true;
      status.textContent='Ввод сохранён для текущей версии. Проверьте значения перед записью.';
    });
    form.addEventListener('submit',event=>{
      event.preventDefault();prepare();save(editor);
    });
  }
  buildEditor('plan','Задать ресурсный план','Записать план');
  buildEditor('actual','Внести факт','Записать факт');
  buildEditor('checkpoint','Зафиксировать прогноз','Сохранить снимок');
  function resetEditor(editor) {
    editor.form.reset();
    editor.status.textContent=''; editor.basis=null; editor.retry=null; editor.busy=false;
    editor.refresh.hidden=true;editor.submit.disabled=false;editor.details.open=false;
  }
  async function save(editor) {
    if (editor.busy || !context || !editor.form.reportValidity()) return;
    const {form,action} = editor;
    const fields = form.elements, basis = editor.basis;
    const source = fields.source.value.trim();
    const payload = {project_id:context.project.id,stage:context.stage.key,action,author:fields.author.value.trim(),
      expected_version:basis.version,fingerprint:basis.fingerprint};
    if (action === 'plan') {payload.date=fields.date.value;payload.plan=planData(form);}
    if (action === 'checkpoint') {payload.date=fields.date.value;payload.source=source;}
    if (action === 'actual') {
      if (!fields.hours.value && !fields.cost.value) {editor.status.textContent='Введите часы или сумму расхода.';fields.hours.focus();return;}
      payload.actual = {date:fields.date.value,role:fields.role.value.trim(),hours:fields.hours.value || null,
        cost:fields.cost.value || null,currency:fields.currency.value.trim().toUpperCase(),source};
    }
    const signature = JSON.stringify(payload);
    if (!editor.retry || editor.retry.signature !== signature) editor.retry={signature,id:crypto.randomUUID()};
    payload.request_id=editor.retry.id;
    const owner=generation;
    editor.busy=true;editor.submit.disabled=true;editor.status.textContent='Сохраняем…';
    try {
      const session=await fetch('/api/ui');
      if (!session.ok) throw Error('Не удалось подключиться к дашборду.');
      const {token}=await session.json();
      const response=await fetch('/api/stage/resource',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-UI-Token':token},body:JSON.stringify(payload)});
      const result=await response.json();
      if (!response.ok) throw Error(result.error || 'Не удалось сохранить запись.');
      if (generation === owner) {
        resetEditor(editor);
        editor.summary.focus();
        editor.status.textContent='Запись сохранена.';
        panel.querySelector('.res-save-result').textContent='Сохранено · ' + actionNames[action] + ' · ' + dateLabel(action === 'actual' ? payload.actual.date : payload.date);
      }
    } catch (error) {
      if (generation === owner) editor.status.textContent=error.message;
    } finally {
      if (generation === owner) {editor.busy=false;editor.submit.disabled=false;}
      document.dispatchEvent(new Event('stage-refresh'));
    }
  }
  const feedback = node('p',null,'res-save-result');
  feedback.setAttribute('role','status'); panel.querySelector('.res-editors').after(feedback);
  function showOptions(list,values) {
    list.replaceChildren(...[...new Set(values.filter(Boolean))].sort().map(value=>new Option(value,value)));
  }
  document.addEventListener('stage-view',event=>{
    const value=event.detail, previous=context;
    if (!value?.stage || !value.resources || !value.decisions?.forecast) {panel.hidden=true;context=null;return;}
    context=value;panel.hidden=false;
    if (!previous || previous.stage.key!==value.stage.key || previous.project.id!==value.project.id) {
      generation++; lastDisplay='';indicatorChosen=false;feedback.textContent='';
      for (const editor of editors.values()) resetEditor(editor);
      part('points').open=false;
    }
    const basis=resourceBasis();
    for (const editor of editors.values()) {
      const stale=editor.basis && (editor.basis.version!==basis.version || editor.basis.fingerprint!==basis.fingerprint);
      editor.refresh.hidden=!stale;
      if (stale && !editor.status.textContent) editor.status.textContent='Данные этапа изменились. Черновик сохранён. Для записи сверьте его с текущей сводкой.';
    }
    const signature=JSON.stringify([value.resources,value.decisions.forecast.fingerprint]);
    if (signature===lastDisplay) return;
    lastDisplay=signature;
    showSummary(value);showHistory();
    const plan=value.resources.plan || {}, work=value.decisions.forecast.resource_work || [];
    showOptions(panel.querySelector('#resource-role-options'),[...work.map(w=>w.role),...(plan.teams || []).map(t=>t.role),...(value.resources.actuals || []).map(a=>a.role)]);
    showOptions(panel.querySelector('#resource-work-options'),work.map(w=>w.key));
    editors.get('plan').summary.textContent=value.resources.plan?.source ? 'Изменить ресурсный план' : 'Задать ресурсный план';
  });
})();

