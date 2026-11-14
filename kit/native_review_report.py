"""Публичная история исправлений. Финальный отчёт сохраняет её вместе с событием finish."""
from __future__ import annotations


REVIEW_TITLES = {
    'pending': 'Awaiting candidate check',
    'checking': 'Checking candidate',
    'passed': 'Check passed',
    'failed': 'Check failed',
    'blocked': 'Check blocked',
    'interrupted': 'Check interrupted',
    'discarded': 'Check discarded',
}


def public_review(review):
    """Оставляем только измерения и короткую диагностику валидатора, без argv и журналов."""
    if not review:
        return None
    visible = {key: review.get(key) for key in (
        'number', 'root_id', 'previous_job', 'status', 'remaining_total', 'remaining_fixer', 'usage_scope')}
    visible['handoff_closed'] = bool(review.get('handoff_closed'))
    visible['status_title'] = REVIEW_TITLES.get(review['status'], 'Unknown check status')
    usage = review.get('usage') or {}
    visible['usage'] = {key: usage[key] for key in ('input_tokens', 'output_tokens')
                        if type(usage.get(key)) is int and usage[key] >= 0}
    check = review.get('check') or {}
    visible['check'] = {key: check[key] for key in ('tests', 'failures', 'errors', 'skipped')
                        if type(check.get(key)) is int and check[key] >= 0}
    for key in ('test_name', 'diagnostic', 'candidate_hash'):
        if isinstance(check.get(key), str):
            visible['check'][key] = ' '.join(check[key].split())[:300]
    if visible['check'].get('diagnostic'):
        from .localization import diagnostic
        message = visible['check']['diagnostic']
        # В первых квитанциях unittest имя проверки повторялось внутри сообщения.
        if check.get('test_name') and message == f"Не выполнено утверждение unittest · {check['test_name']}.":
            message = 'Не выполнено утверждение unittest.'
        visible['check']['diagnostic'] = diagnostic(message, 'en')
    return visible


def report_snapshot(view):
    jobs = []
    incidents = []
    for job in view['jobs']:
        review = public_review(job.get('review'))
        if review:
            jobs.append(dict(id=job['id'], file=job['assigned_file'], role=job['role'],
                             integrated=bool(job.get('integrated')), review=review))
        for incident in job.get('provider_incidents', []):
            incidents.append(dict(incident, job=job['id'], file=job['assigned_file']))
    if jobs or incidents:
        snapshot = dict(agent=view['agent'], jobs=jobs)
        if incidents:
            snapshot['provider_incidents'] = incidents
        return snapshot
    return None


def report_lines(snapshot, t):
    if not snapshot:
        return []

    def cell(value):
        if value is None:
            return t('no data')
        return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')

    grouped = {}
    for job in snapshot['jobs']:
        grouped.setdefault(job['review']['root_id'], []).append(job)
    agent = 'Claude Code' if snapshot['agent'] == 'claude' else 'Codex'
    lines = []
    if snapshot.get('provider_incidents'):
        lines.extend(['### ' + t('Provider interruptions') + ' · ' + agent, ''])
        for incident in snapshot['provider_incidents']:
            reason = 'Provider limit' if incident['reason'] == 'limit' else 'Provider API error'
            details = [cell(incident.get('file') or incident['job']), t('CLI attempt') + ' ' + str(incident['attempt']),
                       t(reason), cell(incident['model'])]
            if incident['partial_size'] is None:
                details.append(t('No file received'))
            else:
                details.extend([t('File saved for inspection'), str(incident['partial_size']) + ' B',
                                'SHA-256 ' + incident['partial_sha256']])
            lines.append('- ' + ' · '.join(details))
        lines.append('')
    if not grouped:
        return lines
    lines.extend(['### ' + t('Candidate checks and repairs') + ' · ' + agent, ''])
    for root_id, jobs in grouped.items():
        lines.extend([cell(jobs[0]['file']) + ' · ' + cell(root_id), '',
            t('| Attempt | Role | Test and outcome | Remaining attempts (total / fixer) | CLI tokens (input / output) |'),
            '|---:|---|---|---|---|'])
        for job in sorted(jobs, key=lambda value: value['review']['number']):
            review = job['review']
            check = review['check']
            details = [t(review['status_title'])]
            if check.get('test_name'):
                details.append(check['test_name'])
            if check.get('tests') is not None:
                details.append(t('Tests run {n}, failures {f}, errors {e}, skipped {s}',
                                 n=check['tests'], f=cell(check.get('failures')),
                                 e=cell(check.get('errors')), s=cell(check.get('skipped'))))
            if check.get('diagnostic'):
                details.append(t(check['diagnostic']))
            if check.get('candidate_hash'):
                details.append('SHA-256 ' + check['candidate_hash'])
            if job.get('integrated'):
                details.append(t('Integrated'))
            remaining = cell(review['remaining_total']) + ' / ' + cell(review['remaining_fixer'])
            usage = review['usage']
            tokens = cell(usage.get('input_tokens')) + ' / ' + cell(usage.get('output_tokens'))
            role = t('Fixer' if job['role'] == 'bug_fixer' else 'Executor')
            lines.append(f"| {review['number']} | {role} | {cell(' · '.join(details))} | {remaining} | {tokens} |")
        lines.append('')
    lines.extend([t('Token counters are shown as reported by each CLI session.'), ''])
    return lines


def review_counts(jobs):
    """Возврат состоялся при постановке связанного поручения исправителю."""
    reviews = [job['review'] for job in jobs if job.get('review')]
    completed = [review for review in reviews if review['status'] in ('passed', 'failed', 'blocked')]
    return dict(reviews=len(completed), reworks=sum(bool(r['previous_job']) for r in reviews),
                passed=sum(r['status'] == 'passed' for r in completed))
