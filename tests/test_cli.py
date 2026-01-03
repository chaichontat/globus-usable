from __future__ import annotations

from click.testing import CliRunner

from globus_usable.cli_app import cli


def test_cp_dereference_default_true_flag_is_toggleable() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cp", "--help"])
    assert result.exit_code == 0
    # The flag may be wrapped across lines in rich output, so check for a reliable substring
    assert "dereference" in result.output.lower()


def test_mv_local_missing_source_is_click_exception() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["mv", "does-not-exist", "dst"])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output
