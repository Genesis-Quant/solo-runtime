import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from solo_runtime.apps import control, execution, factor, model, optimize, strategy
from solo_runtime.manage import main

APPLICATIONS = [("factor", factor), ("model", model), ("optimize", optimize),
                ("control", control), ("execution", execution), ("strategy", strategy)]


@pytest.fixture
def input_file(tmp_path):
    directory = tmp_path / "runs" / str(uuid4())
    directory.mkdir(parents=True)
    return directory / "input.json"


@pytest.mark.parametrize("name,app", APPLICATIONS)
def test_each_command_dispatches_to_its_app(name, app, monkeypatch, input_file):
    handler = Mock(return_value=7)
    monkeypatch.setattr(app, "run", handler)
    assert main(["apps", name, "--input-file", str(input_file)]) == 7
    handler.assert_called_once_with(input_file)


@pytest.mark.parametrize("name,app", APPLICATIONS)
def test_app_selects_scheme_task_contract(name, app, monkeypatch, input_file):
    launcher = Mock(return_value=0)
    monkeypatch.setattr(app, "run_task", launcher)
    assert app.run(input_file) == 0
    launcher.assert_called_once_with(input_file, kind=name)


@pytest.mark.parametrize("name,app", APPLICATIONS)
def test_mismatched_input_fails_before_install(name, app, input_file, capsys):
    source = input_file
    source.write_text(json.dumps({"kind": "backtest" if name == "factor" else "factor"}))
    assert main(["apps", name, "--input-file", str(source)]) == 1
    assert "kind" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["model", "optimize", "control", "execution", "strategy"])
def test_backtest_cannot_replace_task_kind(name, input_file, capsys):
    source = input_file
    source.write_text('{"kind":"backtest","algos":{}}')
    assert main(["apps", name, "--input-file", str(source)]) == 1
    assert name in capsys.readouterr().err
