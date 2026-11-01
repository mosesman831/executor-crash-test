"""Command-line entry point."""
import asyncio

import typer

from crashtest.runner import run_f0
from crashtest.settings import load_settings

app = typer.Typer()


@app.callback()
def main() -> None:
    """Test self-hosted executor resilience."""


@app.command()
def run(fault: str = typer.Option(..., help='Fault identifier (F0).'), keep_session: bool = False) -> None:
    """Run a baseline scenario."""
    if fault.upper() != 'F0':
        typer.echo('Only F0 is currently available.', err=True)
        raise typer.Exit(3)
    try:
        result = asyncio.run(run_f0(load_settings(), keep_session=keep_session))
    except Exception as exc:
        typer.echo(f'Run setup failed: {type(exc).__name__}', err=True)
        raise typer.Exit(2)
    typer.echo(f"{result['status']}: {result['run_dir']}")
    raise typer.Exit({'PASS': 0, 'WARN': 0, 'FAIL': 1}.get(result['status'], 2))
