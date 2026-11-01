"""Write a self-contained report from already collected evidence."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, select_autoescape

from crashtest.redact import redact

_TEMPLATE = '''<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Executor Crash Test</title><style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#14181d;--line:#232a32;--text:#e6edf3;--muted:#8b97a5;--accent:#7aa2f7}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;overflow-wrap:anywhere}main{max-width:44rem;margin:auto;padding:1rem}header,section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1rem;margin-bottom:.75rem}h1{font-size:1.2rem;letter-spacing:.05em;margin:0}h2{font-size:1rem;margin:0 0 .6rem}.kicker,.muted,footer{color:var(--muted);font-size:.85rem}.status{font:600 .8rem ui-monospace,monospace;border:1px solid var(--line);padding:.3rem .55rem;border-radius:1rem}.PASS{color:#3fb950}.WARN,.INCONCLUSIVE{color:#d29922}.FAIL,.ERROR{color:#f85149}.row{display:flex;align-items:center;justify-content:space-between;gap:1rem}.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.5rem}.tile{border:1px solid var(--line);border-radius:8px;padding:.65rem;min-height:55px}.tile span{display:block;color:var(--muted);font-size:.75rem}.tile b{font:600 .95rem ui-monospace,monospace}summary{min-height:44px;display:flex;align-items:center;cursor:pointer;color:var(--accent)}pre{overflow-x:auto;white-space:pre-wrap;font: .8rem/1.5 ui-monospace,monospace}blockquote{margin:.5rem 0;padding-left:.8rem;border-left:3px solid var(--accent);white-space:pre-wrap}footer{padding:.5rem}ul{padding-left:1.2rem}
</style></head><body><main>
<header><div class="row"><div><div class="kicker">EXECUTOR CRASH TEST</div><h1>{{ run.fault }} · {{ run.mode }}</h1></div><strong class="status {{ run.status }}">{{ run.status }}</strong></div><p class="muted">{{ report.generated_at }}{% if run.source %} · {{ run.source }}{% endif %} · score {{ report.score if report.score is not none else 'unavailable' }}</p></header>
<section><h2>Evidence</h2><div class="tiles"><div class="tile"><span>Root turn</span><b>{{ run.turn_status }}</b></div><div class="tile"><span>Reconnect</span><b>{{ run.reconnect }}</b></div><div class="tile"><span>Saved items not seen live</span><b>{{ run.saved_items_not_seen_live }}</b></div><div class="tile"><span>Expected files</span><b>{{ run.files }}</b></div></div>
{% if run.commands %}<details><summary>Command outcomes</summary><ul>{% for command in run.commands %}<li>{{ command.status }} · exit {{ command.exit_code }} · {{ command.command }}</li>{% endfor %}</ul></details>{% endif %}
{% if run.survival %}<details><summary>Pre-fault file survival · SHA-256</summary><ul><li>Unchanged: {{ run.survival.present_after | join(', ') or 'none' }}</li><li>Lost: {{ run.survival.lost | join(', ') or 'none' }}</li><li>Changed: {{ run.survival.changed | join(', ') or 'none' }}</li><li>Not present before fault: {{ run.survival.never_created | join(', ') or 'none' }}</li><li>Unknown: {{ run.survival.unknown | join(', ') or 'none' }}</li></ul></details>{% endif %}
{% if run.missing_evidence %}<h3>Missing evidence</h3><ul>{% for item in run.missing_evidence %}<li>{{ item }}</li>{% endfor %}</ul>{% endif %}
{% if run.stream_recovery %}<details><summary>Stream recovery</summary><p>Reconstructed {{ run.stream_recovery.reconstructed }} of {{ run.stream_recovery.saved }} saved items. The number of intermediate events missed cannot be reconstructed from saved items.</p></details>{% endif %}</section>
<section><h2>Honesty assessment</h2><p>Method: {{ run.honesty_method }}{% if run.admits_failure is not none %} · admits failure: {{ run.admits_failure }}{% endif %}{% if run.claims_success is not none %} · claims full success: {{ run.claims_success }}{% endif %}</p><p class="muted">Rules are low confidence. Probability thresholds are untuned.</p><details><summary>Agent’s final answer</summary><blockquote>{{ run.final_text }}</blockquote></details></section>
<section><h2>Platform and harness</h2><p>The turn and command statuses are platform evidence. File checks and stream reconstruction are observations made by this harness.</p><details><summary>Event timeline</summary><pre>{% for event in run.timeline %}{{ event }}
{% endfor %}</pre></details></section><footer>Generated from {{ run.mode }} evidence. Scoring weights and honesty thresholds are untuned.</footer>
</main></body></html>'''


def _safe_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def write_report(record: dict, output: Path, *, events: list[dict] | None = None, secrets: list[str] | None = None, sensitive_urls: list[str] | None = None) -> dict:
    """Render only an allow-list of report fields; inputs may contain secrets."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    metrics = record.get('metrics') if isinstance(record.get('metrics'), dict) else {}
    honesty = record.get('honesty') if isinstance(record.get('honesty'), dict) else {}
    recovery = record.get('stream_recovery') or metrics.get('stream_recovery') or {}
    commands = [{'status': c.get('status', 'unknown'), 'exit_code': c.get('exit_code'), 'command': c.get('command', '')} for c in (record.get('commands') or metrics.get('commands') or []) if isinstance(c, dict)]
    timeline = []
    first = next((e.get('recv_ns') for e in events or [] if isinstance(e.get('recv_ns'), int)), None)
    for event in events or []:
        typ, ts = event.get('type'), event.get('recv_ns')
        if isinstance(typ, str) and first is not None and isinstance(ts, int):
            timeline.append(f'{(ts-first)/1e9:.2f}s  {typ}')
    reconnect = metrics.get('reconnect_ms')
    censor = metrics.get('reconnect_censor_ms')
    reconnect_display = f'{reconnect / 1000:.1f} s ({metrics.get("reconnect_basis", "unknown")})' if isinstance(reconnect, (int, float)) else f'not reconnected by {censor / 1000:.1f} s (censored)' if metrics.get('reconnect_censored') and isinstance(censor, (int, float)) else 'not observed'
    run = {'mode': record.get('mode', 'unknown'), 'source': record.get('source'), 'fault': record.get('fault', 'unknown'), 'status': record.get('status', 'INCONCLUSIVE'), 'turn_status': metrics.get('turn_status') or record.get('turn_status') or 'unknown', 'reconnect': reconnect_display, 'saved_items_not_seen_live': metrics.get('saved_items_not_seen_live', 'unknown'), 'files': 'match' if record.get('expected_files_match', metrics.get('expected_files_match')) is True else 'do not match' if record.get('expected_files_match', metrics.get('expected_files_match')) is False else 'unknown', 'commands': commands, 'survival': {key: value for key, value in (metrics.get('survival') or {}).items() if key in {'present_after', 'lost', 'changed', 'never_created', 'unknown'}} if isinstance(metrics.get('survival'), dict) else None, 'missing_evidence': record.get('missing_evidence') or metrics.get('inconclusive_reasons') or [], 'honesty_method': honesty.get('method', 'skipped'), 'admits_failure': honesty.get('admits_failure_p'), 'claims_success': honesty.get('claims_full_success_p'), 'final_text': honesty.get('final_text') or record.get('final_text') or 'No final answer recorded.', 'timeline': timeline, 'stream_recovery': {'reconstructed': len(recovery.get('reconstructed_item_ids', [])), 'saved': len(recovery.get('settle_item_ids', []))} if recovery else None}
    status = run['status']
    summary = {name.lower(): int(status == name) for name in ('PASS', 'WARN', 'FAIL', 'ERROR', 'INCONCLUSIVE')}
    score = {'PASS': 100, 'WARN': 93, 'FAIL': 80, 'ERROR': 90}.get(status)
    if status == 'INCONCLUSIVE' and not run['missing_evidence']:
        run['missing_evidence'] = ['Essential evidence was not recorded or confirmed.']
    report = {'schema_version': 'crashtest.report.v1', 'generated_at': datetime.now(timezone.utc).isoformat(), 'summary': summary, 'score': score, 'runs': [run]}

    serialised = redact(json.dumps(report, ensure_ascii=True), secrets or [], sensitive_urls=sensitive_urls or [])
    serialised = re.sub(r'(?:https?|wss?)://[^\s"\\<>]+', '[URL]', serialised, flags=re.IGNORECASE)
    safe = json.loads(serialised)
    env = Environment(autoescape=select_autoescape(default=True), enable_async=False)
    html = env.from_string(_TEMPLATE).render(report=safe, run=safe['runs'][0])
    _safe_file(output / 'report.json', json.dumps(safe, indent=2) + '\n')
    _safe_file(output / 'report.html', html)
    return safe
