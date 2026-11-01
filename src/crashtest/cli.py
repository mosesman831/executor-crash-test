"""Command-line entry point."""
import asyncio
import json
import tempfile
from pathlib import Path

import typer

from crashtest.runner import run_f0
from crashtest.replay import replay as replay_run
from crashtest.report import write_report
from crashtest.settings import load_settings

app = typer.Typer()


@app.callback()
def main() -> None:
    """Test self-hosted executor resilience."""


@app.command()
def run(fault: str = typer.Option(..., help='Fault identifier (F0, F1 or F3).'), keep_session: bool = False, no_judge: bool = False) -> None:
    """Run a baseline scenario."""
    if fault.upper() not in {'F0', 'F1', 'F3'}:
        typer.echo('Only F0, F1 and F3 are currently available.', err=True)
        raise typer.Exit(3)
    try:
        result = asyncio.run(run_f0(load_settings(), keep_session=keep_session, fault=fault.upper(), no_judge=no_judge))
    except Exception as exc:
        typer.echo(f'Run setup failed: {type(exc).__name__}', err=True)
        raise typer.Exit(2)
    typer.echo(f"{result['status']}: {result['run_dir']}")
    raise typer.Exit({'PASS': 0, 'WARN': 0, 'FAIL': 1, 'INCONCLUSIVE': 3}.get(result['status'], 2))


@app.command()
def replay(fixture_dir: Path, speed: float = typer.Option(8.0, help='Replay speed multiplier.'), output: Path | None = typer.Option(None, help='Report output directory.')) -> None:
    """Replay a redacted recording without network access."""
    try:
        typer.echo('mode: replay')
        result = asyncio.run(replay_run(fixture_dir, speed=speed))
        output_dir = output or Path(tempfile.mkdtemp(prefix='crashtest-replay-'))
        events_file = fixture_dir / 'events.jsonl'
        events = [json.loads(line) for line in events_file.read_text(encoding='utf-8').splitlines() if line.strip()] if events_file.exists() else []
        write_report(result, output_dir, events=events)
    except Exception as exc:
        typer.echo(f'Replay failed: {type(exc).__name__}', err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(result, indent=2))
    typer.echo(f'Report: {output_dir / "report.html"}')
    raise typer.Exit({'PASS': 0, 'WARN': 0, 'FAIL': 1, 'ERROR': 2, 'INCONCLUSIVE': 3}.get(result['status'], 2))
