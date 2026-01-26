from __future__ import annotations

import pytest

import globus_usable.cli_app as cli_app


class _Prompt:
    def __init__(self, result: object):
        self._result = result

    def ask(self) -> object:
        return self._result


def test_search_for_more_only_shows_additional_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = [
        ("Alpha", "id-alpha"),
        ("Beta", "id-beta"),
    ]

    def fake_discover(*, scope: str = "my-endpoints", fulltext: str | None = None, limit: int = 1000):
        assert scope == "all"
        assert fulltext == "alp"
        assert limit == 200
        return [
            ("Alpha", "id-alpha"),  # already present in initial
            ("Gamma", "id-gamma"),  # new
        ]

    monkeypatch.setattr(cli_app, "_discover_accessible_linked_collections", fake_discover)

    checkbox_calls: list[tuple[str, list[object]]] = []
    main_checkbox_calls = 0

    def fake_checkbox(message: str, *, choices: list[object]):
        nonlocal main_checkbox_calls
        checkbox_calls.append((message, choices))

        if "additional" in message.lower():
            return _Prompt(["id-gamma"])

        main_checkbox_calls += 1
        if main_checkbox_calls == 1:
            return _Prompt(["id-alpha", "id-beta"])
        return _Prompt(["id-alpha", "id-beta", "id-gamma"])

    confirm_results = iter([True, False])

    def fake_confirm(message: str, *, default: bool):
        assert default is False
        return _Prompt(next(confirm_results))

    def fake_text(message: str):
        assert "Search term" in message
        return _Prompt("alp")

    monkeypatch.setattr(cli_app.questionary, "checkbox", fake_checkbox)
    monkeypatch.setattr(cli_app.questionary, "confirm", fake_confirm)
    monkeypatch.setattr(cli_app.questionary, "text", fake_text)

    selected = cli_app._select_linked_collections_interactive(initial)
    assert [cid for _, cid in selected] == ["id-alpha", "id-beta", "id-gamma"]

    additional_prompts = [
        (message, choices) for message, choices in checkbox_calls if "additional" in message.lower()
    ]
    assert len(additional_prompts) == 1
    _, additional_choices = additional_prompts[0]

    # The "additional" prompt must not include already-added collections/endpoints.
    additional_values = [getattr(choice, "value", choice) for choice in additional_choices]
    assert additional_values == ["id-gamma"]
