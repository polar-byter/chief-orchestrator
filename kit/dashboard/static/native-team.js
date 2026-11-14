'use strict';

const NativeTeamPanel = (() => {
  const node = id => document.getElementById(id);
  const statuses = {
    queued:'In queue', claimed:'Preparing launch', running:'Running', prepared:'Result saved',
    completed:'Isolated work completed', interrupted:'Needs a decision', cancelled:'Cancelled',
    failed:'Turn failed', stopping:'Waiting for process confirmation', paused:'Team paused', finished:'Finished'
  };
  const roles = {chief:'Chief', executor:'Executor', bug_fixer:'Bug Fixer'};
  const captions = {
    run:'Run queue', clear:'Clear queue', recover:'Reconcile processes', drop:'Remove from queue',
    retry:'Queue another attempt', discard:'Discard attempt', publish:'Publish saved candidate',
    integrate:'Integrate into project', recheck:'Run check again'
  };
  let project = null, team = null, mode = null, updatingMode = false, pendingTask = '';
  let translate = text => text, refresh = () => {}, signature = '';

  function setModeView() {
    const control = node('chief-mode-toggle');
    const transition = {enabling:'Finish enabling Chief', disabling:'Finish disabling Chief'};
    control.disabled = !mode || updatingMode;
    control.setAttribute('aria-checked', String(Boolean(mode?.enabled)));
    control.textContent = translate(transition[mode?.status] || 'Chief mode');
    node('chief-mode-state').textContent = translate(updatingMode ? 'Applying project mode…' :
      (mode?.status === 'on' ? 'On for this project' : mode?.status === 'off' ? 'Off for this project' :
        mode ? 'Mode change requires completion' : 'Reading project settings…'));
  }
  async function post(url, data) {
    const access = await fetch('/api/ui', {cache:'no-store'});
    if (!access.ok) throw new Error(translate('Cannot read dashboard data'));
    const {token} = await access.json();
    const response = await fetch(url, {method:'POST', headers:{
      'Content-Type':'application/json', 'X-Dashboard-UI-Token':token,
      'Accept-Language':document.documentElement.lang
    }, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || response.statusText);
    return result;
  }
  function button(parent, label, action, job) {
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = translate(label);
    item.dataset.teamAction = action;
    if (job) item.dataset.jobId = job.id;
    item.disabled = pendingTask === team.task;
    item.addEventListener('click', () => act(action, job));
    parent.append(item);
  }
  function incidentRow(job, incident) {
    const row = document.createElement('div');
    row.className = 'provider-incident';
    const reason = incident.reason === 'limit' ? 'Provider limit' : 'Provider API error';
    const details = [translate('CLI attempt') + ' ' + incident.attempt, translate(reason)];
    if (incident.model) details.push(incident.model);
    row.textContent = details.join(' · ');
    if (incident.partial_size == null) {
      row.append(' · ' + translate('No file received'));
      return row;
    }
    const save = document.createElement('button');
    save.type = 'button';
    save.textContent = translate('Download file for inspection') + ' (' + incident.partial_size + ' B)';
    save.title = 'SHA-256 ' + incident.partial_sha256;
    const address = {project, task:team.task, job_id:job.id, attempt:incident.attempt};
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const result = await post('/api/native-partial', address);
        const bytes = Uint8Array.from(atob(result.content), byte => byte.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type:'application/octet-stream'}));
        const download = document.createElement('a');
        download.href = url;
        download.download = job.assigned_file.split('/').pop() + '.attempt-' + incident.attempt + '.txt';
        download.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        if (team?.task === address.task) node('native-team-feedback').textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    row.append(' · ', save);
    return row;
  }
  function attemptRow(job, current) {
    const item = document.createElement('li'), label = document.createElement('strong');
    item.dataset.jobId = job.id;
    const review = job.review;
    const role = review && job.role === 'bug_fixer' ? 'Fixer' : roles[job.role];
    label.textContent = review ? translate('Attempt') + ' ' + review.number + ' · ' + translate(role)
      : translate(roles[job.role]) + ' · ' + (job.assigned_file || '#' + job.id.slice(0,8));
    label.title = job.id;
    const state = document.createElement('span');
    state.className = 'muted';
    let status = job.status === 'completed' && job.role === 'chief' ? 'Reply received' : statuses[job.status];
    if (review && (job.status === 'completed' || ['checking','interrupted','blocked'].includes(review.status))) {
      status = review.status_title;
    }
    state.textContent = ' · ' + translate(status) + (job.integrated ? ' · ' + translate('Integrated') : '') +
      (!review && job.attempt ? ' · ' + translate('Attempt') + ' ' + job.attempt : '');
    item.append(label, state);
    for (const incident of job.provider_incidents || []) item.append(incidentRow(job, incident));
    if (review) {
      item.dataset.verdict = review.status;
      const check = review.check;
      const measures = document.createElement('div');
      measures.className = 'muted';
      const budget = translate('Remaining attempts (total / fixer)') + ' ' +
        review.remaining_total + ' / ' + review.remaining_fixer;
      const counters = review.usage || {};
      const tokens = translate('CLI tokens (input / output)') + ' ' +
        (counters.input_tokens ?? '—') + ' / ' + (counters.output_tokens ?? '—');
      measures.textContent = budget + ' · ' + tokens;
      item.append(measures);
      if (review.status === 'failed' && (review.handoff_closed || !review.remaining_total || !review.remaining_fixer)) {
        const reason = document.createElement('div');
        reason.textContent = translate(review.handoff_closed ? 'Fixer handoff cancelled' : 'Repair budget exhausted');
        item.append(reason);
      }
      if (check?.test_name || check?.diagnostic || Number.isInteger(check?.tests)) {
        const detail = document.createElement('div');
        const counts = Number.isInteger(check.tests) ? translate('Tests run {n}, failures {f}, errors {e}, skipped {s}')
          .replace('{n}', check.tests).replace('{f}', check.failures ?? '—')
          .replace('{e}', check.errors ?? '—').replace('{s}', check.skipped ?? '—') : '';
        detail.textContent = [check.test_name, counts, check.diagnostic && translate(check.diagnostic)].filter(Boolean).join(' · ');
        item.append(detail);
      }
    }
    if (current.status === 'finished') return item;
    const links = document.createElement('div');
    links.className = 'actions';
    if (job.status === 'queued') button(links, captions.drop, 'drop', job);
    if (job.status === 'interrupted' && current.status === 'paused') {
      button(links, review?.status === 'interrupted' ? captions.recheck : captions.retry, 'retry', job);
      if (job.prepared && review?.status !== 'interrupted') button(links, captions.publish, 'publish', job);
      button(links, captions.discard, 'discard', job);
    }
    if (review?.status === 'blocked' && current.status === 'running' && mode?.enabled) {
      button(links, captions.recheck, 'recheck', job);
    }
    if (job.status === 'completed' && job.assigned_file && !job.integrated && mode?.enabled &&
        (!review || review.status === 'passed')) button(links, captions.integrate, 'integrate', job);
    if (links.children.length) item.append(links);
    return item;
  }
  function renderJobs(list, current) {
    const chains = new Map();
    list.replaceChildren();
    for (const job of current.jobs) {
      const root = job.review?.root_id;
      if (!root) {
        list.append(attemptRow(job, current));
        continue;
      }
      if (!chains.has(root)) {
        const group = document.createElement('li'), name = document.createElement('strong');
        group.className = 'repair-chain';
        group.dataset.reviewRoot = root;
        name.textContent = job.assigned_file;
        name.title = root;
        const attempts = document.createElement('ol');
        group.append(name, attempts);
        list.append(group);
        chains.set(root, attempts);
      }
      chains.get(root).append(attemptRow(job, current));
    }
  }
  function render(current, t) {
    translate = t;
    if (team?.task !== current?.task) node('native-team-feedback').textContent = '';
    team = current;
    const panel = node('native-team');
    panel.hidden = !current;
    if (!current) {signature = ''; return;}
    const stamp = JSON.stringify([current, mode?.status, pendingTask, document.documentElement.lang]);
    if (stamp === signature) return;
    signature = stamp;
    node('native-team-title').textContent = translate('Project team') + ' · ' + (current.agent === 'codex' ? 'Codex' : 'Claude Code');
    const waiting = current.jobs.filter(job => job.status === 'queued').length;
    const active = current.jobs.filter(job => ['claimed','running','prepared'].includes(job.status) || job.review?.status === 'checking').length;
    const closed = ['paused','cancelled','finished'].includes(current.status);
    const parts = [translate(statuses[current.status]), translate('In queue') + ' ' + waiting,
                   translate('Active turns') + ' ' + active];
    if (current.reason === 'limit') parts.push(translate('Provider limit'));
    if (current.reason === 'api_error') parts.push(translate('Provider API error'));
    if (current.status === 'paused' && current.jobs.some(job => job.status === 'interrupted')) {
      parts.push(translate('Resolve interrupted turns before resuming.'));
    }
    node('native-team-state').textContent = parts.join(' · ');
    const actions = node('native-team-actions');
    actions.replaceChildren();
    const handedOff = new Set(current.jobs.map(job => job.review?.previous_job).filter(Boolean));
    const awaitingReview = current.jobs.some(job => job.status === 'completed' && (
      job.review?.status === 'pending' || job.review?.status === 'failed' && !job.review.handoff_closed && !handedOff.has(job.id) &&
      job.review.remaining_total > 0 && job.review.remaining_fixer > 0));
    if (current.status === 'running' && (waiting || awaitingReview) && mode?.enabled) button(actions, captions.run, 'run');
    if (waiting && current.status !== 'finished') button(actions, captions.clear, 'clear');
    if (!closed && (active || current.status === 'stopping')) button(actions, captions.recover, 'recover');
    const list = node('native-team-jobs');
    renderJobs(list, current);
    if (!current.jobs.length) {
      const empty = document.createElement('li');
      empty.textContent = translate('No assignments yet.');
      list.append(empty);
    }
  }
  async function act(action, job) {
    if (!team || !project || pendingTask) return;
    const task = team.task, data = {project, task, action, generation:team.generation};
    if (job) Object.assign(data, {job_id:job.id, attempt:job.attempt, boundary:job.boundary});
    pendingTask = task;
    render(team, translate);
    node('native-team-feedback').textContent = translate('Sending action…');
    try {
      const response = await post('/api/native-team', data);
      if (team?.task === task) {
        node('native-team-feedback').textContent = translate('Command confirmed.');
        render(response, translate);
      }
    } catch (error) {
      if (team?.task === task) node('native-team-feedback').textContent = error.message;
    } finally {
      pendingTask = '';
      render(team, translate);
      refresh();
    }
  }
  async function loadMode(id, t, readJSON) {
    translate = t;
    const data = await readJSON('/api/chief-mode');
    if (data.project !== id) throw new Error(t('This page belongs to a different project'));
    project = id;
    if (!updatingMode) mode = data;
    setModeView();
  }
  function mount(update) {
    refresh = update;
    node('chief-mode-toggle').addEventListener('click', async () => {
      if (!mode || !project || updatingMode) return;
      // После прерывания оператор завершает прежний переход в том же направлении.
      const enabled = mode.status === 'enabling' || mode.status === 'off';
      updatingMode = true;
      setModeView();
      try {
        mode = await post('/api/chief-mode', {project, enabled});
        node('settings-message').textContent = '';
      } catch (error) {
        node('settings-message').textContent = error.message;
      } finally {
        updatingMode = false;
        setModeView();
        render(team, translate);
        refresh();
      }
    });
  }
  return {mount, loadMode, render, act, enabled:() => Boolean(mode?.enabled)};
})();
