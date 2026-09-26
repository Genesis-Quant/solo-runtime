"""启动锁定的任务环境；不导入或解析任何研究接口。"""

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_result(output: Path, input_sha256: str, lock_sha256: str) -> None:
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    if manifest.get("protocol") != 1 or manifest.get("status") != "success":
        raise ValueError("完成清单的协议或状态无效")
    if manifest.get("input_sha256") != input_sha256:
        raise ValueError("完成清单与本次输入不一致")
    if manifest.get("lock_sha256") != lock_sha256:
        raise ValueError("完成清单与本次任务锁文件不一致")
    reports = manifest.get("reports")
    hashes = manifest.get("report_sha256")
    if not isinstance(reports, dict) or not reports or not isinstance(hashes, dict):
        raise ValueError("完成清单缺少报告文件及哈希")
    for filename in reports.values():
        path = (output / filename).resolve()
        if Path(filename).is_absolute() or not path.is_relative_to(output):
            raise ValueError("报告文件必须位于输出目录内")
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"报告文件缺失或为空：{filename}")
        if file_hash(path) != hashes.get(filename):
            raise ValueError(f"报告文件哈希不一致：{filename}")


def run_task(input_file: Path, *, kind: str) -> int:
    input_file = input_file.resolve()
    source = input_file.read_bytes()
    data = json.loads(source)
    if data.get("kind") != kind:
        raise ValueError(f"任务输入 kind 必须为 {kind}")
    # 只读取启动信封，其他字段由任务环境中的研究入口解释。
    project_lock = (input_file.parent / data["environment"]["lockfile"]).resolve()
    output = (input_file.parent / data["output"]).resolve()
    project = project_lock.parent
    if project_lock.name != "uv.lock" or not (project / "pyproject.toml").is_file():
        raise ValueError("任务 uv.lock 旁必须有 pyproject.toml")
    if (output / "run.json").exists():
        raise FileExistsError("输出目录已有成功运行记录，请使用新的 Run/Attempt 目录")
    lock = tomllib.loads(project_lock.read_text(encoding="utf-8"))
    if any(
        {"editable", "directory"} & package.get("source", {}).keys()
        for package in lock.get("package", [])
    ):
        raise ValueError("正式任务不能依赖可变源码目录，请先构建 wheel 再锁定")
    input_sha256 = hashlib.sha256(source).hexdigest()
    lock_sha256 = file_hash(project_lock)
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(project / ".venv"))
    # Worker 的全局镜像设置不能覆盖任务锁文件所使用的索引。
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX", "UV_EXTRA_INDEX_URL"):
        env.pop(key, None)
    result = subprocess.run(
        ["uv", "sync", "--project", str(project), "--locked", "--no-editable", "--no-dev"],
        cwd=project,
        env=env,
    )
    if result.returncode:
        return result.returncode
    entry = project / ".venv" / ("Scripts/scheme.exe" if os.name == "nt" else "bin/scheme")
    if not entry.is_file():
        raise FileNotFoundError("任务环境没有提供 scheme 命令入口")
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(project),
            "--no-sync",
            str(entry),
            "run",
            "--input",
            str(input_file),
            "--output",
            str(output),
        ],
        cwd=project,
        env=env,
    )
    if result.returncode:
        return result.returncode
    if file_hash(project_lock) != lock_sha256 or file_hash(input_file) != input_sha256:
        raise ValueError("运行期间输入或锁文件发生变化")
    check_result(output, input_sha256, lock_sha256)
    return 0
