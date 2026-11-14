# Dashboard

The dashboard is a read-only view of a task: it reads the journal
(`.metrics/<ID>/events.jsonl`) and OpenCode sessions; `kit/task_metrics.py` owns the rules.
Start with README.ru.md.

- Operational code: `launcher.py` (start, reuse, stop), `runtime.py` (JSON API and
  `static/runtime.html`), `tracking.py`, `observations.py`, `delivery.py`, `controls.py`,
  `session_control.py`, `quota_monitor.py`.
- `journal.py` reads the journal for the views (`read_events`, `brief_summary`).
  Loop guard rules live only in `kit/loop_guards.py`; do not duplicate them here.
- `console.py`, `simple.py` and `static/simple.html` are small hand-written examples
  from learning Python. Keep them working and keep the user's own comments in them.
- The page stays plain HTML/JavaScript without frameworks.
- Use meaningful English identifiers. Write new or revised kit comments and
  docstrings in natural Russian, explaining state transitions or constraints
  where the reason is easy to miss. Preserve the author's learning notes.
- Run the dashboard tests from the project root:
  `py -m unittest discover -s kit/dashboard/tests -t .`

Use `/stage` for stage work. Route stage reads through `stage_monitor`;
discussion and trace writes belong to `stage_collaboration`, estimate decisions
to `stage_decisions`. Keep the task journal read-only in these views. Preserve
the selected stage, feature and document revision between team tabs.
Stage resource arithmetic belongs to `stage_resources`, dated inputs and
immutable snapshots to `stage_resource_ledger`. A running dashboard observes
changed estimates for stages with an existing resource journal. GET requests
only read. Keep partial totals, registered zero and missing data distinct.
Read aggregated risks from the stage estimate. Keep the original source with
each risk discussion. The browser edits the named working document using its
hash and record revision. Reuse the operation UUID after an uncertain response.
