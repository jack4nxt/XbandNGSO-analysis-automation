"""CLI entry point: xband-batch parse-list | preflight | freeze | run | post | status."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import typer

from .adapters import load_adapter
from .config import BatchConfig, PathsConfig, PostBatchConfig, load_model
from .coordination import run_parse_list
from .errors import BatchError, PreflightBlockedError
from .freeze import freeze_batch
from .paths import Layout
from .post import run_batch_post
from .preflight import run_preflight
from .runner import BatchRunner, BatchState

app = typer.Typer(add_completion=False, help="X-band NGSO batch interference analysis")


def _default_paths_yaml(batch_path: Path) -> Path:
    return batch_path.resolve().parent / "paths.yaml"


def _load_context(batch_path: Path, paths_path: Path | None):
    batch_path = batch_path.resolve()
    p_path = (paths_path or _default_paths_yaml(batch_path)).resolve()
    paths_cfg = load_model(PathsConfig, p_path)
    batch_cfg = load_model(BatchConfig, batch_path)
    layout = Layout(paths_cfg)
    return batch_cfg, batch_path.parent, layout


@app.command("parse-list")
def parse_list_cmd(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml (default: sibling of batch)"),
):
    """Parse, normalize, match, and classify coordination lists."""
    try:
        batch_cfg, base_dir, layout = _load_context(batch, paths)
        catalog = load_adapter(batch_cfg.engine.catalog_adapter, batch_cfg.engine.options)
        report = run_parse_list(batch_cfg, base_dir, layout, catalog)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"total={report.total_rows} counts={report.counts_by_category}")
    typer.echo(f"table:  {layout.coordination_table_path(batch_cfg.batch_id)}")
    typer.echo(f"report: {layout.validation_report_path(batch_cfg.batch_id)}")


@app.command()
def preflight(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml"),
):
    """Run antenna & readiness gatekeeper; exits non-zero if any item is pending."""
    try:
        batch_cfg, base_dir, layout = _load_context(batch, paths)
        engine = load_adapter(batch_cfg.engine.engine_adapter, batch_cfg.engine.options)
        report = run_preflight(batch_cfg, base_dir, layout, engine)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"pairs={len(report.pair_plans)} pending_antennas={len(report.pending_items)} "
               f"unresolved_coord={report.unresolved_coordination_count}")
    typer.echo(f"report: {report.pending_md_path}")
    if not report.passed:
        raise typer.Exit(code=1)


@app.command()
def freeze(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml"),
):
    """Freeze all pair inputs after preflight passes."""
    try:
        batch_cfg, base_dir, layout = _load_context(batch, paths)
        engine = load_adapter(batch_cfg.engine.engine_adapter, batch_cfg.engine.options)
        res = freeze_batch(batch_cfg, batch.resolve(), base_dir, layout, engine)
    except PreflightBlockedError as exc:
        typer.echo(f"BLOCKED: {exc}", err=True)
        raise typer.Exit(code=1)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"frozen {len(res.frozen_pairs)} pair(s) -> {res.manifest_path}")


@app.command()
def run(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml"),
):
    """Execute batch simulation from frozen inputs only."""
    try:
        batch_cfg, base_dir, layout = _load_context(batch, paths)
        engine = load_adapter(batch_cfg.engine.engine_adapter, batch_cfg.engine.options)
        state = BatchRunner(engine).run(batch_cfg, batch.resolve(), base_dir, layout)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    counts = dict(Counter(p.status for p in state.pairs))
    typer.echo(f"batch {state.batch_id} status={counts}")


@app.command()
def post(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    post_yaml: Path = typer.Option(..., "--post", help="Path to post_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml"),
):
    """Run standalone batch post-processing from raw outputs only."""
    try:
        batch_cfg, _base_dir, layout = _load_context(batch, paths)
        post_cfg = load_model(PostBatchConfig, post_yaml.resolve())
        engine = load_adapter(batch_cfg.engine.engine_adapter, batch_cfg.engine.options)
        out = run_batch_post(batch_cfg, post_cfg, post_yaml.resolve(), layout, engine)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"post_dir: {out}")


@app.command()
def status(
    batch: Path = typer.Option(..., "--batch", help="Path to batch_*.yaml"),
    paths: Path = typer.Option(None, "--paths", help="Path to paths.yaml"),
):
    """Show current batch execution status."""
    try:
        batch_cfg, _base_dir, layout = _load_context(batch, paths)
        state_path = layout.batch_state_path(batch_cfg.batch_id)
        if not state_path.is_file():
            typer.echo("batch not started yet (no batch_state.json)")
            raise typer.Exit(code=0)
        state = BatchState.load(state_path)
    except BatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=2)
    counts = dict(Counter(p.status for p in state.pairs))
    typer.echo(f"batch_id={state.batch_id} start_utc={state.start_utc} counts={counts}")
    for p in state.pairs:
        if p.status == "failed":
            typer.echo(f"  FAILED {p.pair_dir_name}: {p.error}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
