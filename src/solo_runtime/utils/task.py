"""启动锁定的任务环境；不导入或解析任何研究接口。"""

import hashlib
import json
import os
import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path
from uuid import UUID

_ADMISSION_TIMEOUT = 15
_ADMISSION_BODY_LIMIT = 64 * 1024


def _admission_reason(body: bytes) -> str:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    detail = data.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("reason")
    if not isinstance(detail, str):
        return ""
    reason = "".join(char if char.isprintable() else " " for char in detail)
    reason = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[redacted URL]", reason)
    reason = re.sub(
        r"(?i)\b[\w-]*(?:token|password|passwd|secret|api[_-]?key|authorization)[\w-]*"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+\S+|[^\s,;]+)",
        "[redacted credentials]",
        reason,
    )
    reason = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", "[redacted credentials]", reason)
    return " ".join(reason.split())[:1000]


def _check_task_admission(
    run_id: UUID, *, kind: str, input_sha256: str, lock_sha256: str,
) -> None:
    backend_url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").strip().rstrip("/")
    try:
        url = urllib.parse.urlsplit(backend_url)
        if (
            url.scheme not in {"http", "https"} or not url.hostname or url.port == 0
            or url.username is not None or url.password is not None or url.query or url.fragment
            or any(not char.isprintable() for char in backend_url)
        ):
            raise ValueError
    except ValueError:
        raise ValueError("SOLO_BACKEND_URL 必须是不带凭据、查询或片段的 HTTP(S) Backend 地址") from None
    request = urllib.request.Request(
        f"{backend_url}/api/v1/version-policy/tasks/{run_id}/check",
        data=json.dumps({
            "kind": kind, "input_sha256": input_sha256, "lock_sha256": lock_sha256,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_ADMISSION_TIMEOUT) as response:
            if response.status != 200:
                raise RuntimeError(f"Backend 任务准入请求失败 (HTTP {response.status})，未启动任务")
            body = response.read(_ADMISSION_BODY_LIMIT + 1)
    except urllib.error.HTTPError as error:
        reason = ""
        with error:
            if error.code == 422:
                try:
                    reason = _admission_reason(error.read(_ADMISSION_BODY_LIMIT))
                except (OSError, HTTPException):
                    pass
        detail = f"：{reason}" if reason else ""
        raise RuntimeError(
            f"Backend 任务准入请求失败 (HTTP {error.code}){detail}，未启动任务",
        ) from None
    except (urllib.error.URLError, OSError, HTTPException, ValueError, UnicodeError):
        raise RuntimeError("Backend 任务准入服务不可用或请求超时，未启动任务") from None
    try:
        if len(body) > _ADMISSION_BODY_LIMIT:
            raise ValueError
        admission = json.loads(body)
    except (ValueError, UnicodeError):
        raise RuntimeError("Backend 任务准入响应无效，未启动任务") from None
    if isinstance(admission, dict) and admission.get("allowed") is False:
        raise RuntimeError("Backend 拒绝任务准入，未启动任务")
    if (
        not isinstance(admission, dict) or admission.get("allowed") is not True
        or not isinstance(admission.get("grandfathered"), bool)
    ):
        raise RuntimeError("Backend 任务准入响应无效，未启动任务")


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
    try:
        run_id = UUID(input_file.parent.name)
    except ValueError:
        raise ValueError("正式任务需要 Backend 管理的 Run UUID：input.json 的父目录名必须为 UUID") from None
    # 只读取启动信封，其他字段由任务环境中的研究入口解释。
    project_lock = (input_file.parent / data["environment"]["lockfile"]).resolve()
    output = (input_file.parent / data["output"]).resolve()
    project = project_lock.parent
    if project_lock.name != "uv.lock" or not (project / "pyproject.toml").is_file():
        raise ValueError("任务 uv.lock 旁必须有 pyproject.toml")
    if (output / "run.json").exists():
        raise FileExistsError("输出目录已有成功运行记录，请使用新的 Run/Attempt 目录")
    lock_source = project_lock.read_bytes()
    lock = tomllib.loads(lock_source.decode("utf-8"))
    if any(
        {"editable", "directory"} & package.get("source", {}).keys()
        for package in lock.get("package", [])
    ):
        raise ValueError("正式任务不能依赖可变源码目录，请先构建 wheel 再锁定")
    input_sha256 = hashlib.sha256(source).hexdigest()
    lock_sha256 = hashlib.sha256(lock_source).hexdigest()
    # 准入只依据原始输入、锁文件和 Backend 提交记录；退休后的历史任务由 Backend 裁决。
    _check_task_admission(
        run_id, kind=kind, input_sha256=input_sha256, lock_sha256=lock_sha256,
    )
    if file_hash(input_file) != input_sha256 or file_hash(project_lock) != lock_sha256:
        raise ValueError("任务准入期间输入或锁文件发生变化")
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
