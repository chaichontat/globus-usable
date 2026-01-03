from __future__ import annotations

import pytest

from globus_usable.errors import GlobusUsableError
from globus_usable.globus_cli import parse_json


def test_parse_json_empty_behavior() -> None:
    assert parse_json("", "ctx", empty="empty") == {}
    with pytest.raises(GlobusUsableError):
        parse_json("", "ctx")

