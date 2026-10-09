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
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

_ADMISSION_TIMEOUT = 15
_ADMISSION_BODY_LIMIT = 64 * 1024
_ARTIFACT_LIMIT = 256
_ARTIFACT_NAME_LIMIT = 200
_ARTIFACT_VERSION_LIMIT = 128
_ARTIFACT_PATH_LIMIT = 1024
_RESEARCH_PACKAGE = re.compile(r"(?:factor|model|optimize|control|execution|strategy)-[0-9a-f]{4}")


def _admission_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复的准入字段")
        result[key] = value
    return result


def _artifact_manifest(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > _ARTIFACT_LIMIT:
        raise ValueError("无效的本地 wheel 清单")
    names = set()
    paths = set()
    artifacts = []
    for artifact in value:
        if not isinstance(artifact, dict) or set(artifact) != {"package", "version", "sha256", "wheel"}:
            raise ValueError("无效的本地 wheel 清单字段")
        name, version, digest, wheel = (artifact[key] for key in ("package", "version", "sha256", "wheel"))
        if (
            not isinstance(name, str) or len(name) > _ARTIFACT_NAME_LIMIT
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or not isinstance(version, str) or len(version) > _ARTIFACT_VERSION_LIMIT
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            or not isinstance(wheel, str) or not wheel or len(wheel) > _ARTIFACT_PATH_LIMIT
        ):
            raise ValueError("无效的本地 wheel 清单值")
        path = PurePosixPath(wheel)
        if (
            path.is_absolute() or path.as_posix() != wheel or "\\" in wheel or ":" in wheel
            or any(part in {".", ".."} or part.endswith((".", " ")) for part in wheel.split("/"))
            or not all(char.isprintable() for char in wheel) or path.suffix != ".whl"
        ):
            raise ValueError("本地 wheel 清单路径必须为 Run 内的 POSIX 相对路径")
        path_key = os.path.normcase(wheel)
        if name in names or path_key in paths:
            raise ValueError("本地 wheel 清单身份或路径重复")
        names.add(name)
        paths.add(path_key)
        artifacts.append(dict(artifact, sha256=digest.lower()))
    return artifacts


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
) -> list[dict[str, str]]:
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
        admission = json.loads(body, object_pairs_hook=_admission_object)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError("Backend 任务准入响应无效，未启动任务") from None
    if isinstance(admission, dict) and admission.get("allowed") is False:
        raise RuntimeError("Backend 拒绝任务准入，未启动任务")
    if (
        not isinstance(admission, dict) or admission.get("allowed") is not True
        or not isinstance(admission.get("grandfathered"), bool)
    ):
        raise RuntimeError("Backend 任务准入响应无效，未启动任务")
    try:
        # 旧的纯 registry 锁文件允许省略清单；本地锁文件仍须匹配完整快照。
        return _artifact_manifest(admission.get("artifacts", []))
    except ValueError:
        raise RuntimeError("Backend 任务准入响应无效：本地 wheel 清单无效，未启动任务") from None


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _local_path(value: object, base: Path) -> Path:
    if (
        not isinstance(value, str) or not value or len(value) > 4096
        or not all(char.isprintable() for char in value)
    ):
        raise ValueError("锁文件的本地 wheel 路径无效")
    path = Path(value)
    if path.is_absolute():
        return path
    url = urllib.parse.urlsplit(value)
    if url.query or url.fragment or url.netloc not in {"", "localhost"}:
        raise ValueError("锁文件的本地 wheel 路径无效")
    if url.scheme == "file":
        path = Path(urllib.request.url2pathname(url.path))
    elif not url.scheme and not url.netloc and not PureWindowsPath(value).is_absolute():
        path = Path(urllib.parse.unquote(url.path))
    else:
        raise ValueError("锁文件的本地 wheel 路径无效")
    return path if path.is_absolute() else base / path


def _own_run_path(path: Path, run_directory: Path) -> Path:
    try:
        parts = path.relative_to(run_directory).parts
        current = run_directory
        # 检查每一段，不能先 resolve 后丢失曾经越出 Run 的符号链接。
        for part in parts:
            current /= part
            if not current.resolve(strict=True).is_relative_to(run_directory):
                raise ValueError("本地 wheel 路径或符号链接必须位于本次 Run 内")
        return current.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("本地 wheel 路径缺失、不可读取或符号链接无效") from None
    except ValueError:
        raise ValueError("本地 wheel 路径或符号链接必须位于本次 Run 内") from None


def _locked_artifacts(lock: dict, project: Path, run_directory: Path) -> dict[str, dict[str, str]]:
    artifacts = {}
    names = []
    paths = set()
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        raise ValueError("任务锁文件的包清单无效")
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("source", {}), dict):
            raise ValueError("任务锁文件的包清单无效")
        name = package.get("name")
        if (
            not isinstance(name, str) or len(name) > _ARTIFACT_NAME_LIMIT
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name)
        ):
            raise ValueError("任务锁文件的包名称无效")
        name = re.sub(r"[-_.]+", "-", name).lower()
        names.append(name)
        source = package.get("source", {})
        registry = source.get("registry")
        if registry is not None and not isinstance(registry, str):
            raise ValueError("任务锁文件的 registry 无效")
        local_registry = registry is not None and (
            Path(registry).is_absolute() or PureWindowsPath(registry).is_absolute()
            or urllib.parse.urlsplit(registry).scheme in {"", "file"}
        )
        if "path" not in source and not local_registry:
            direct_url = source.get("url")
            if isinstance(direct_url, str) and (
                Path(direct_url).is_absolute() or PureWindowsPath(direct_url).is_absolute()
                or urllib.parse.urlsplit(direct_url).scheme in {"", "file"}
            ):
                raise ValueError("本地 wheel 必须使用可核验的 path 或 registry 锁条目")
            if _RESEARCH_PACKAGE.fullmatch(name):
                raise ValueError("研究包必须使用本次 Run 内的本地 wheel 和 Backend 接受快照")
            continue
        if "path" in source and registry is not None:
            raise ValueError("任务锁文件的本地 wheel 来源冲突")
        version = package.get("version")
        if not isinstance(version, str) or not version or len(version) > _ARTIFACT_VERSION_LIMIT:
            raise ValueError("本地 wheel 的锁定版本无效")
        wheels = package.get("wheels", [])
        if not isinstance(wheels, list) or (wheels and (len(wheels) != 1 or not isinstance(wheels[0], dict))):
            raise ValueError("本地 wheel 锁条目必须唯一")
        locked_wheel = wheels[0] if wheels else {}
        if "path" in source:
            wheel = _own_run_path(_local_path(source["path"], project), run_directory)
        else:
            registry_path = _local_path(registry, project)
            if not _own_run_path(registry_path, run_directory).is_dir():
                raise ValueError("本地 wheel registry 必须为本次 Run 内的目录")
            if len(wheels) != 1 or ("path" in locked_wheel) == ("url" in locked_wheel):
                raise ValueError("本地 wheel registry 必须锁定唯一的 wheel 文件")
            reference = locked_wheel.get("path", locked_wheel.get("url"))
            base = registry_path if "path" in locked_wheel else project
            wheel = _own_run_path(_local_path(reference, base), run_directory)
        if wheel.suffix != ".whl" or not wheel.is_file():
            raise ValueError("本地 wheel 文件缺失或类型无效")
        relative = wheel.relative_to(run_directory).as_posix()
        if name in artifacts or relative in paths:
            raise ValueError("任务锁文件的本地 wheel 包身份或路径冲突")
        artifact = {"package": name, "version": version, "wheel": relative}
        if "hash" in locked_wheel:
            digest = locked_wheel["hash"]
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                raise ValueError("任务锁文件的本地 wheel 哈希无效")
            artifact["sha256"] = digest[7:].lower()
        artifacts[name] = artifact
        paths.add(relative)
        if len(artifacts) > _ARTIFACT_LIMIT:
            raise ValueError("任务锁文件的本地 wheel 清单过大")
    if any(names.count(name) != 1 for name in artifacts):
        raise ValueError("任务锁文件的本地 wheel 包身份冲突")
    return artifacts


def _verify_artifacts(
    artifacts: list[dict[str, str]], lock: dict, project: Path, run_directory: Path,
) -> None:
    locked = _locked_artifacts(lock, project, run_directory)
    if {artifact["package"] for artifact in artifacts} != locked.keys():
        raise ValueError("Backend 本地 wheel 接受快照缺失或与任务锁文件不一致")
    for artifact in artifacts:
        expected = locked[artifact["package"]]
        if (
            artifact["version"] != expected["version"] or artifact["wheel"] != expected["wheel"]
            or ("sha256" in expected and artifact["sha256"] != expected["sha256"])
        ):
            raise ValueError("Backend 本地 wheel 接受快照与任务锁文件的身份、路径或哈希不一致")
        path = _own_run_path(run_directory / artifact["wheel"], run_directory)
        try:
            digest = file_hash(path)
        except OSError:
            raise ValueError("本地 wheel 文件缺失或不可读取") from None
        if digest != artifact["sha256"]:
            raise ValueError("本地 wheel 哈希与 Backend 接受快照不一致")


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
    artifacts = _check_task_admission(
        run_id, kind=kind, input_sha256=input_sha256, lock_sha256=lock_sha256,
    )
    if file_hash(input_file) != input_sha256 or file_hash(project_lock) != lock_sha256:
        raise ValueError("任务准入期间输入或锁文件发生变化")
    _verify_artifacts(artifacts, lock, project, input_file.parent)
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(project / ".venv"),
               UV_CACHE_DIR=str(input_file.parent.parent / ".uv-cache"), UV_LINK_MODE="hardlink")
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
    if file_hash(input_file) != input_sha256 or file_hash(project_lock) != lock_sha256:
        raise ValueError("环境同步期间输入或锁文件发生变化")
    _verify_artifacts(artifacts, lock, project, input_file.parent)
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
    _verify_artifacts(artifacts, lock, project, input_file.parent)
    check_result(output, input_sha256, lock_sha256)
    return 0
