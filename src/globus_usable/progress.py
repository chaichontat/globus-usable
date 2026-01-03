from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Literal

from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn

from .errors import GlobusUsableError
from .globus_cli import Runner, parse_json
from .log import logger
from .metrics import clamp_percent
from .units import MIB

PollMode = Literal["interactive", "json", "quiet"]

STATUS_ACTIVE = "ACTIVE"
STATUS_FAILED = "FAILED"
NICE_STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    label: str
    src: str | None = None
    dst: str | None = None


@dataclass(frozen=True)
class PollResult:
    task_data: dict[str, dict]
    errors: dict[str, str]


def _emit_ndjson(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@dataclass
class _PollState:
    labels: dict[str, str]
    task_data: dict[str, dict]
    errors: dict[str, str]
    seen_events: dict[str, set[tuple[str, str, str | None, str | None]]]
    active: set[str]
    last_progress: dict[str, tuple[int, int]]
    warned_done_over_total: set[str]
    interval: float


def _is_already_completed_cancel_error(exc: GlobusUsableError) -> bool:
    text = str(exc).lower()
    return (
        "not active" in text
        or "already completed" in text
        or "already inactive" in text
        or "not cancellable" in text
        or "cannot be cancelled" in text
        or "cannot be canceled" in text
    )


def latest_error_message(runner: Runner, task_id: str) -> str | None:
    out = runner(
        "task",
        "event-list",
        "--filter-errors",
        "--limit",
        "1",
        "--format",
        "json",
        task_id,
    )
    events = parse_json(out, f"reading error event for task {task_id}").get("DATA", [])
    if not events:
        return None
    ev = events[0]
    desc = ev.get("description") or ev.get("code") or "Error"
    details = ev.get("details")
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
            body = parsed.get("error", {}).get("body") if isinstance(parsed, dict) else None
            if body:
                details = body
        except json.JSONDecodeError:
            pass
    return f"{desc}: {details}" if details else desc


def next_poll_interval(
    current: float, poll_min: float, poll_max: float, progress_made: bool
) -> float:
    if progress_made:
        return poll_min
    return min(poll_max, max(poll_min, current * 1.5))


def _run_poll_loop(
    runner: Runner,
    *,
    state: _PollState,
    mode: PollMode,
    poll_min: float,
    poll_max: float,
    abort_on_error: bool,
    progress_ui: Progress | None,
    progress_tasks: dict[str, int],
) -> None:
    while state.active:
        progress_made = False
        for task_id in list(state.active):
            out = runner("task", "show", task_id, "--format", "json")
            t = parse_json(out, f"reading task {task_id}")
            state.task_data[task_id] = t

            bytes_transferred = int(t.get("bytes_transferred") or 0)
            total = int(t.get("subtasks_total") or 0)
            done = int(t.get("subtasks_succeeded") or 0)
            eff_bps = float(t.get("effective_bytes_per_second") or 0)

            prev_bytes, prev_done = state.last_progress.get(task_id, (0, 0))
            if bytes_transferred > prev_bytes or done > prev_done:
                progress_made = True
            state.last_progress[task_id] = (bytes_transferred, done)

            speed_str = f"{eff_bps/MIB:.1f} MB/s" if eff_bps else "--"
            eta_str = "--"
            estimated_total = None
            if total > 0 and eff_bps and done:
                done_for_eta = min(done, total)
                est_total = bytes_transferred * total / done_for_eta
                eta_s = max(0.0, (est_total - bytes_transferred) / eff_bps)
                eta_str = f"{int(eta_s // 60)}m"
                estimated_total = int(est_total)

            if progress_ui:
                if estimated_total is not None and bytes_transferred > estimated_total:
                    estimated_total = bytes_transferred
                progress_ui.update(
                    progress_tasks[task_id],
                    total=estimated_total,
                    completed=bytes_transferred,
                    speed=speed_str,
                    eta=eta_str,
                )
            elif mode == "json":
                percent = clamp_percent(done, total)
                _emit_ndjson(
                    {
                        "type": "progress",
                        "task_id": task_id,
                        "bytes": bytes_transferred,
                        "percent": percent,
                    }
                )

            if total > 0 and done > total and task_id not in state.warned_done_over_total:
                state.warned_done_over_total.add(task_id)
                logger.warning(f"warning: task {task_id} reports done {done} > total {total}")

            status_text = (t.get("nice_status") or "").upper()
            is_error = NICE_STATUS_ERROR in status_text or t.get("status") == STATUS_FAILED
            if is_error and task_id not in state.errors:
                msg = latest_error_message(runner, task_id) or "Transfer failed"
                state.errors[task_id] = msg
                if progress_ui:
                    progress_ui.update(progress_tasks[task_id], description=f"[red]{msg}")
                elif mode == "json":
                    _emit_ndjson({"type": "error", "task_id": task_id, "message": msg})
                if abort_on_error:
                    cancel_failures: list[str] = []
                    for tid in list(state.active):
                        try:
                            runner("task", "cancel", tid)
                        except GlobusUsableError as exc:
                            if _is_already_completed_cancel_error(exc):
                                continue
                            cancel_failures.append(f"{tid}: {exc}")
                    if cancel_failures:
                        details = "; ".join(cancel_failures[:3])
                        if len(cancel_failures) > 3:
                            details = f"{details}; ..."
                        raise GlobusUsableError(f"{msg} (also failed to cancel: {details})")
                    raise GlobusUsableError(msg)

            if t.get("status") != STATUS_ACTIVE:
                if progress_ui:
                    desc = state.errors.get(task_id)
                    progress_ui.update(
                        progress_tasks[task_id],
                        description=f"[red]{t.get('status')}: {desc}"
                        if desc
                        else f"[green]{t.get('status')}",
                    )
                elif mode == "json":
                    _emit_ndjson(
                        {
                            "type": "completed",
                            "task_id": task_id,
                            "files": t.get("files"),
                            "bytes": t.get("bytes_transferred"),
                        }
                    )
                state.active.discard(task_id)

            events_out = runner(
                "task",
                "event-list",
                "--limit",
                "100",
                "--format",
                "json",
                task_id,
            )
            events_payload = parse_json(events_out, f"reading event log for task {task_id}")
            events = events_payload.get("DATA", [])
            if events:
                seen_for_task = state.seen_events.setdefault(task_id, set())
                label = state.labels.get(task_id, task_id)
                for ev in reversed(events):
                    key = (
                        ev.get("time", ""),
                        ev.get("code", ""),
                        ev.get("description"),
                        ev.get("details"),
                    )
                    if key in seen_for_task:
                        continue
                    seen_for_task.add(key)

                    if mode == "json":
                        _emit_ndjson(
                            {
                                "type": "event",
                                "task_id": task_id,
                                "code": ev.get("code"),
                                "time": ev.get("time"),
                                "description": ev.get("description"),
                            }
                        )
                    elif progress_ui:
                        code = ev.get("code", "")
                        description = ev.get("description") or ""
                        time_str = ev.get("time", "")
                        is_ev_error = bool(ev.get("is_error"))
                        parts = [f"[{label}] {code}"]
                        if description and description.lower() != code.lower():
                            parts.append(f"- {description}")
                        if time_str:
                            parts.append(f"@ {time_str}")
                        progress_ui.console.log(
                            " ".join(parts), style="red" if is_ev_error else "cyan"
                        )

        state.interval = next_poll_interval(state.interval, poll_min, poll_max, progress_made)
        if state.active:
            time.sleep(state.interval)


def poll_tasks(
    runner: Runner,
    tasks: Iterable[TaskSpec],
    *,
    mode: PollMode,
    poll_min: float,
    poll_max: float,
    abort_on_error: bool,
) -> PollResult:
    tasks = list(tasks)
    state = _PollState(
        labels={t.task_id: t.label for t in tasks},
        task_data={},
        errors={},
        seen_events={},
        active={t.task_id for t in tasks},
        last_progress={t.task_id: (0, 0) for t in tasks},
        warned_done_over_total=set(),
        interval=poll_min,
    )

    if mode == "json":
        for t in tasks:
            _emit_ndjson(
                {
                    "type": "started",
                    "task_id": t.task_id,
                    "src": t.src,
                    "dst": t.dst,
                }
            )
        _run_poll_loop(
            runner,
            state=state,
            mode=mode,
            poll_min=poll_min,
            poll_max=poll_max,
            abort_on_error=abort_on_error,
            progress_ui=None,
            progress_tasks={},
        )
        return PollResult(task_data=state.task_data, errors=state.errors)

    if mode == "quiet":
        _run_poll_loop(
            runner,
            state=state,
            mode=mode,
            poll_min=poll_min,
            poll_max=poll_max,
            abort_on_error=abort_on_error,
            progress_ui=None,
            progress_tasks={},
        )
        return PollResult(task_data=state.task_data, errors=state.errors)

    if mode != "interactive":
        raise ValueError(f"Unknown mode: {mode}")

    progress_tasks: dict[str, int] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}", justify="right"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        DownloadColumn(),
        TextColumn("ETA {task.fields[eta]}", justify="right"),
        TextColumn("{task.fields[speed]}", justify="right"),
    ) as progress_ui:
        for t in tasks:
            progress_tasks[t.task_id] = progress_ui.add_task(
                t.label, total=None, speed="--", eta="--"
            )
        _run_poll_loop(
            runner,
            state=state,
            mode=mode,
            poll_min=poll_min,
            poll_max=poll_max,
            abort_on_error=abort_on_error,
            progress_ui=progress_ui,
            progress_tasks=progress_tasks,
        )

    return PollResult(task_data=state.task_data, errors=state.errors)
