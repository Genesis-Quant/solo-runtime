"""Solo 管理命令，与 Arena 一样按 apps 分发任务。"""

import argparse
from collections.abc import Sequence

from . import apps

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solo-manage", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("apps", help="运行研究应用", add_help=False)
    arguments, remaining = parser.parse_known_args(argv)
    return apps.main(remaining, prog=f"{parser.prog} {arguments.command}")
