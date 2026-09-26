"""组合优化研究任务。"""

from pathlib import Path

from solo_runtime.utils.task import run_task

__all__ = ["run"]


def run(input_file: Path) -> int:
    return run_task(input_file, kind="optimize")
