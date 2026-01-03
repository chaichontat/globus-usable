from __future__ import annotations


def cli() -> None:
    from .cli_app import cli as _cli

    _cli()


__all__ = ["cli"]

