from __future__ import annotations

import json
from collections import defaultdict

import pytest

from globus_usable.progress import TaskSpec, poll_tasks
from globus_usable.errors import GlobusUsableError


def test_poll_tasks_json_emits_started_event_completed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)
    calls = defaultdict(int)

    def runner(*args: str) -> str:
        if args[:2] == ("task", "show"):
            task_id = args[2]
            calls[task_id] += 1
            if calls[task_id] == 1:
                return json.dumps(
                    {
                        "status": "ACTIVE",
                        "nice_status": "ACTIVE",
                        "bytes_transferred": 100,
                        "subtasks_total": 10,
                        "subtasks_succeeded": 1,
                        "effective_bytes_per_second": 1024,
                    }
                )
            return json.dumps(
                {
                    "status": "SUCCEEDED",
                    "nice_status": "SUCCEEDED",
                    "bytes_transferred": 1000,
                    "subtasks_total": 10,
                    "subtasks_succeeded": 10,
                    "effective_bytes_per_second": 1024,
                    "files": 2,
                }
            )
        if args[:2] == ("task", "event-list"):
            return json.dumps({"DATA": [{"code": "SUCCEEDED", "time": "t", "description": "done"}]})
        raise AssertionError(f"Unexpected args: {args}")

    poll_tasks(
        runner,
        [TaskSpec(task_id="T1", label="x", src="a", dst="b")],
        mode="json",
        poll_min=0.0,
        poll_max=0.0,
        abort_on_error=False,
    )

    out = capsys.readouterr().out.strip().splitlines()
    events = [json.loads(line) for line in out if line.strip()]
    types = {e["type"] for e in events}
    assert "started" in types
    assert "completed" in types


def test_poll_tasks_json_clamps_percent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)
    calls = 0

    def runner(*args: str) -> str:
        nonlocal calls
        if args[:2] == ("task", "show"):
            calls += 1
            status = "ACTIVE" if calls == 1 else "SUCCEEDED"
            return json.dumps(
                {
                    "status": status,
                    "nice_status": status,
                    "bytes_transferred": 100,
                    "subtasks_total": 10,
                    "subtasks_succeeded": 11,
                    "effective_bytes_per_second": 1024,
                }
            )
        if args[:2] == ("task", "event-list"):
            return json.dumps({"DATA": []})
        raise AssertionError(args)

    poll_tasks(
        runner,
        [TaskSpec(task_id="T1", label="x")],
        mode="json",
        poll_min=0.0,
        poll_max=0.0,
        abort_on_error=False,
    )
    out = capsys.readouterr().out.strip().splitlines()
    events = [json.loads(line) for line in out if line.strip()]
    percents = [e["percent"] for e in events if e.get("type") == "progress"]
    assert percents and max(percents) == 100.0


def test_poll_tasks_abort_on_error_cancels_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)
    canceled: list[str] = []

    def runner(*args: str) -> str:
        if args[:2] == ("task", "show"):
            if args[2] == "BAD":
                return json.dumps(
                    {
                        "status": "FAILED",
                        "nice_status": "ERROR",
                        "bytes_transferred": 0,
                        "subtasks_total": 1,
                        "subtasks_succeeded": 0,
                        "effective_bytes_per_second": 0,
                    }
                )
            return json.dumps(
                {
                    "status": "ACTIVE",
                    "nice_status": "ACTIVE",
                    "bytes_transferred": 0,
                    "subtasks_total": 1,
                    "subtasks_succeeded": 0,
                    "effective_bytes_per_second": 0,
                }
            )
        if args[:3] == ("task", "event-list", "--filter-errors"):
            return json.dumps({"DATA": [{"description": "Permission denied"}]})
        if args[:2] == ("task", "event-list"):
            return json.dumps({"DATA": []})
        if args[:2] == ("task", "cancel"):
            canceled.append(args[2])
            return ""
        raise AssertionError(args)

    with pytest.raises(GlobusUsableError, match="Permission denied"):
        poll_tasks(
            runner,
            [TaskSpec(task_id="BAD", label="bad"), TaskSpec(task_id="OK", label="ok")],
            mode="quiet",
            poll_min=0.0,
            poll_max=0.0,
            abort_on_error=True,
        )
    assert set(canceled) == {"BAD", "OK"}


def test_poll_tasks_abort_on_error_includes_cancel_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)

    def runner(*args: str) -> str:
        if args[:2] == ("task", "show"):
            return json.dumps(
                {
                    "status": "FAILED",
                    "nice_status": "ERROR",
                    "bytes_transferred": 0,
                    "subtasks_total": 1,
                    "subtasks_succeeded": 0,
                    "effective_bytes_per_second": 0,
                }
            )
        if args[:3] == ("task", "event-list", "--filter-errors"):
            return json.dumps({"DATA": [{"description": "boom"}]})
        if args[:2] == ("task", "event-list"):
            return json.dumps({"DATA": []})
        if args[:2] == ("task", "cancel"):
            raise GlobusUsableError("cancel failed")
        raise AssertionError(args)

    with pytest.raises(GlobusUsableError, match="failed to cancel"):
        poll_tasks(
            runner,
            [TaskSpec(task_id="T1", label="x")],
            mode="quiet",
            poll_min=0.0,
            poll_max=0.0,
            abort_on_error=True,
        )


def test_poll_tasks_abort_on_error_ignores_already_completed_cancel_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)
    canceled: list[str] = []

    def runner(*args: str) -> str:
        if args[:2] == ("task", "show"):
            if args[2] == "BAD":
                return json.dumps(
                    {
                        "status": "FAILED",
                        "nice_status": "ERROR",
                        "bytes_transferred": 0,
                        "subtasks_total": 1,
                        "subtasks_succeeded": 0,
                        "effective_bytes_per_second": 0,
                    }
                )
            return json.dumps(
                {
                    "status": "ACTIVE",
                    "nice_status": "ACTIVE",
                    "bytes_transferred": 0,
                    "subtasks_total": 1,
                    "subtasks_succeeded": 0,
                    "effective_bytes_per_second": 0,
                }
            )
        if args[:3] == ("task", "event-list", "--filter-errors"):
            return json.dumps({"DATA": [{"description": "boom"}]})
        if args[:2] == ("task", "event-list"):
            return json.dumps({"DATA": []})
        if args[:2] == ("task", "cancel"):
            canceled.append(args[2])
            raise GlobusUsableError("Task is not ACTIVE")
        raise AssertionError(args)

    with pytest.raises(GlobusUsableError, match="boom") as excinfo:
        poll_tasks(
            runner,
            [TaskSpec(task_id="BAD", label="bad"), TaskSpec(task_id="OK", label="ok")],
            mode="quiet",
            poll_min=0.0,
            poll_max=0.0,
            abort_on_error=True,
        )
    assert "failed to cancel" not in str(excinfo.value).lower()
    assert set(canceled) == {"BAD", "OK"}


def test_poll_tasks_aborts_on_file_not_found_event_even_without_abort_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("globus_usable.progress.time.sleep", lambda _: None)
    canceled: list[str] = []
    calls = defaultdict(int)

    def runner(*args: str) -> str:
        if args[:2] == ("task", "show"):
            task_id = args[2]
            calls[task_id] += 1
            status = "ACTIVE" if calls[task_id] == 1 else "SUCCEEDED"
            done = 0 if status == "ACTIVE" else 1
            return json.dumps(
                {
                    "status": status,
                    "nice_status": status,
                    "bytes_transferred": 0,
                    "subtasks_total": 1,
                    "subtasks_succeeded": done,
                    "effective_bytes_per_second": 0,
                }
            )
        if args[:2] == ("task", "event-list"):
            task_id = args[-1]
            if task_id == "BAD":
                return json.dumps(
                    {"DATA": [{"code": "FILE_NOT_FOUND", "description": "FILE_NOT_FOUND", "is_error": True}]}
                )
            return json.dumps({"DATA": []})
        if args[:2] == ("task", "cancel"):
            canceled.append(args[2])
            return ""
        raise AssertionError(args)

    with pytest.raises(GlobusUsableError, match="FILE_NOT_FOUND"):
        poll_tasks(
            runner,
            [TaskSpec(task_id="BAD", label="bad"), TaskSpec(task_id="OK", label="ok")],
            mode="quiet",
            poll_min=0.0,
            poll_max=0.0,
            abort_on_error=False,
        )
    assert set(canceled) == {"BAD", "OK"}
