from pathlib import Path

import pytest
from labwire.bridges.pylabrobot import cli
from typer.testing import CliRunner

runner = CliRunner()

TARGET = "labwire.bridges.pylabrobot.tests_support:build"


@pytest.fixture(autouse=True)
def _support_module(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Expose the test rig as an importable factory for the CLI to load."""
    import sys
    import types

    from conftest import CHANNELS
    from pylabrobot.liquid_handling import LiquidHandler
    from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
    from pylabrobot.resources import Cor_96_wellplate_360ul_Fb
    from pylabrobot.resources.hamilton import STARLetDeck, hamilton_96_tiprack_1000uL_filter

    async def build() -> LiquidHandler:
        deck = STARLetDeck()
        handler = LiquidHandler(
            backend=LiquidHandlerChatterboxBackend(num_channels=CHANNELS), deck=deck
        )
        await handler.setup()
        deck.assign_child_resource(hamilton_96_tiprack_1000uL_filter(name="tips"), rails=1)
        deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="source_plate"), rails=7)
        return handler

    module = types.ModuleType("labwire.bridges.pylabrobot.tests_support")
    module.build = build  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "labwire.bridges.pylabrobot.tests_support", module)


def test_check_reports_the_deck_and_every_safety_class() -> None:
    result = runner.invoke(cli.app, ["check", TARGET])
    assert result.exit_code == 0, result.output
    assert "S2  aspirate" in result.output
    assert "S0  stop" in result.output
    assert "source_plate: plate 8x12" in result.output
    assert "tips: tip_rack 8x12  (96 tips)" in result.output


def test_check_surfaces_hazards_and_locks(tmp_path: Path) -> None:
    path = tmp_path / "labwire-pylabrobot.yaml"
    path.write_text(
        "version: 1\nresources:\n  source_plate:\n    hazard: corrosive\n    locked: true\n"
    )
    result = runner.invoke(cli.app, ["check", TARGET, "-a", str(path)])
    assert result.exit_code == 0, result.output
    assert "hazard: corrosive" in result.output
    assert "LOCKED" in result.output


def test_check_reports_an_excluded_command_as_excluded(tmp_path: Path) -> None:
    path = tmp_path / "labwire-pylabrobot.yaml"
    path.write_text("version: 1\ncommands:\n  transfer: {exclude: true}\n")
    result = runner.invoke(cli.app, ["check", TARGET, "-a", str(path)])
    assert result.exit_code == 0, result.output
    assert "transfer (excluded by annotation)" in result.output


def test_a_bad_annotation_file_exits_nonzero(tmp_path: Path) -> None:
    path = tmp_path / "labwire-pylabrobot.yaml"
    path.write_text("version: 1\nresources:\n  plate: {hazzard: corrosive}\n")
    result = runner.invoke(cli.app, ["check", TARGET, "-a", str(path)])
    assert result.exit_code == 1


def test_a_target_without_a_colon_is_rejected_with_the_expected_form() -> None:
    result = runner.invoke(cli.app, ["check", "labwire.bridges.pylabrobot"])
    assert result.exit_code != 0
    assert "module:factory" in result.output


def test_an_unimportable_target_is_reported() -> None:
    result = runner.invoke(cli.app, ["check", "no.such.module:build"])
    assert result.exit_code != 0
    assert "cannot import" in result.output


def test_a_missing_factory_attribute_is_reported() -> None:
    result = runner.invoke(cli.app, ["check", "labwire.bridges.pylabrobot:nonexistent"])
    assert result.exit_code != 0
    assert "nonexistent" in result.output
