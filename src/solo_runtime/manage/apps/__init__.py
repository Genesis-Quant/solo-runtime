"""注册应用命令；各应用对应独立的调度任务。"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from solo_runtime.apps import control, execution, factor, model, optimize, strategy

__all__ = ["main"]


def main(argv: Sequence[str] | None = None, *, prog: str = "solo-manage apps") -> int:
    parser = argparse.ArgumentParser(prog=prog, allow_abbrev=False)
    commands = parser.add_subparsers(dest="app", required=True)
    for name, application in (("factor", factor), ("model", model), ("optimize", optimize),
                              ("control", control), ("execution", execution), ("strategy", strategy)):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--input-file", type=Path, required=True)
        command.set_defaults(handler=application.run)
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments.input_file)
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
