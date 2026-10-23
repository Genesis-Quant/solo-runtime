"""启动锁定的任务环境；不导入或解析任何研究接口。"""

import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path
from uuid import UUID

_ADMISSION_TIMEOUT = 15


class TaskInputError(ValueError):
    """任务启动信封不符合用户输入契约。"""


def _admission_reason(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if not isinstance(detail, dict) or not isinstance(detail.get("reason"), str):
        return ""
    reason = "".join(char if char.isprintable() else " " for char in detail["reason"])
    reason = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[redacted URL]", reason)
    reason = re.sub(
        r"(?i)\b[\w-]*(?:token|password|passwd|secret|api[_-]?key|authorization)[\w-]*"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+\S+|[^\s,;]+)",
        "[redacted credentials]",
        reason,
    )
    reason = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", "[redacted credentials]", reason)
    return " ".join(reason.split())


def _check_task_admission(run_id: UUID, *, kind: str) -> None:
    backend_url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").strip().rstrip("/")
    try:
        request = urllib.request.Request(
            f"{backend_url}/api/v1/version-policy/tasks/{run_id}/check",
            data=json.dumps({"kind": kind}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_ADMISSION_TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"Backend 任务准入请求失败 (HTTP {response.status})，未启动任务")
            body = response.read()
    except urllib.error.HTTPError as error:
        with error:
            try:
                reason = _admission_reason(error.read()) if error.code == 422 else ""
            except (OSError, HTTPException):
                raise RuntimeError("Backend 任务准入服务不可用或请求超时，未启动任务") from None
        detail = f"：{reason}" if reason else ""
        raise RuntimeError(
            f"Backend 任务准入请求失败 (HTTP {error.code}){detail}，未启动任务",
        ) from None
    except (OSError, HTTPException, ValueError):
        raise RuntimeError("Backend 任务准入服务不可用或请求超时，未启动任务") from None
    try:
        admission = json.loads(body)
    except ValueError:
        raise RuntimeError("Backend 任务准入响应无效，未启动任务") from None
    if not isinstance(admission, dict) or admission.get("allowed") is not True:
        raise RuntimeError("Backend 任务准入响应无效，未启动任务")


def check_result(output: Path) -> None:
    output = output.resolve()
    manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
    if manifest.get("protocol") != 1 or manifest.get("status") != "success":
        raise ValueError("完成清单的协议或状态无效")
    reports = manifest.get("reports")
    if not isinstance(reports, dict) or not reports:
        raise ValueError("完成清单的 reports 必须是非空映射")
    for filename in reports.values():
        report = (output / filename).resolve()
        if not report.is_relative_to(output):
            raise ValueError(f"报告文件不在输出目录内：{filename}")
        if not report.is_file():
            raise ValueError(f"报告文件缺失：{filename}")


def run_task(input_file: Path, *, kind: str) -> int:
    input_file = input_file.resolve()
    data = json.loads(input_file.read_text(encoding="utf-8"))
    if data.get("kind") != kind:
        raise TaskInputError(f"任务输入 kind 必须为 {kind}")
    try:
        run_id = UUID(input_file.parent.name)
    except ValueError:
        raise TaskInputError("正式任务需要 Backend 管理的 Run UUID：input.json 的父目录名必须为 UUID") from None
    # 只读取启动信封，其他字段由任务环境中的研究入口解释。
    project_lock = (input_file.parent / data["environment"]["lockfile"]).resolve()
    output = (input_file.parent / data["output"]).resolve()
    project = project_lock.parent
    if project_lock.name != "uv.lock" or not (project / "pyproject.toml").is_file():
        raise TaskInputError("任务 uv.lock 旁必须有 pyproject.toml")
    if (output / "run.json").exists():
        raise TaskInputError("输出目录已有成功运行记录，请使用新的 Run/Attempt 目录")
    lock = tomllib.loads(project_lock.read_text(encoding="utf-8"))
    if any(
        {"editable", "directory"} & package.get("source", {}).keys()
        for package in lock.get("package", [])
    ):
        raise TaskInputError("正式任务不能依赖可变源码目录，请先构建 wheel 再锁定")
    _check_task_admission(run_id, kind=kind)
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(project / ".venv"),
               UV_LINK_MODE=os.environ.get("UV_LINK_MODE") or "hardlink")
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
    check_result(output)
    return 0
