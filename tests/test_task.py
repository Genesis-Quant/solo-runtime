import builtins
import hashlib
import io
import json
import os
import shutil
import traceback
import urllib.error
import zipfile
from http.client import HTTPException
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from solo_runtime.manage import main
from solo_runtime.utils import task

KINDS = ["factor", "model", "optimize", "control", "execution", "strategy"]
ALLOWED = b'{"allowed":true,"grandfathered":false}'


def write_run(directory: Path, *, kind: str = "factor") -> Path:
    project = directory / "environment"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "locked-task"\nversion = "1.0.0"\n', encoding="utf-8",
    )
    (project / "uv.lock").write_bytes(
        b'version = 1\r\nrequires-python = ">=3.12"\r\n\r\n'
        b'[[package]]\r\nname = "scheme"\r\nversion = "1.0.0"\r\n'
        b'source = { registry = "https://pypi.org/simple" }\r\n',
    )
    input_file = directory / "input.json"
    data = {
        "kind": kind, "environment": {"lockfile": "environment/uv.lock"},
        "output": "report", "research": {"note": "原始输入"},
    }
    input_file.write_bytes(
        (json.dumps(data, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n")
        .encode("utf-8"),
    )
    return input_file


@pytest.fixture
def input_file(tmp_path):
    return write_run(tmp_path / "runs" / str(uuid4()))


@pytest.fixture(autouse=True)
def external_calls(monkeypatch):
    monkeypatch.delenv("SOLO_BACKEND_URL", raising=False)
    calls = SimpleNamespace(
        open=Mock(side_effect=AssertionError("Tests must mock Backend admission")),
        process=Mock(side_effect=AssertionError("Tests must not run uv or real tasks")),
    )
    monkeypatch.setattr(task.urllib.request, "urlopen", calls.open)
    monkeypatch.setattr(task.subprocess, "run", calls.process)
    return calls


def respond(calls, body=ALLOWED, *, status=200):
    response = MagicMock(status=status)
    response.__enter__.return_value = response
    response.read.return_value = body
    calls.open.side_effect = None
    calls.open.return_value = response
    return response


def assert_not_started(calls, input_file):
    calls.process.assert_not_called()
    assert not (input_file.parent / "environment/.venv").exists()
    assert not (input_file.parent / "report").exists()


def artifact_response(artifacts, *, grandfathered=False):
    return json.dumps({
        "allowed": True, "grandfathered": grandfathered, "artifacts": artifacts,
    }).encode()


def add_lock_package(input_file, *, name, version, source, wheels=None):
    def inline_table(values):
        return "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + " }"

    package = (
        f"\n[[package]]\nname = {json.dumps(name)}\nversion = {json.dumps(version)}\n"
        f"source = {inline_table(source)}\n"
    )
    if wheels is not None:
        package += "wheels = [" + ", ".join(inline_table(wheel) for wheel in wheels) + "]\n"
    lock = input_file.parent / "environment/uv.lock"
    lock.write_bytes(lock.read_bytes() + package.encode())


def write_local_wheel(
    input_file, *, name="factor-abcd", locked_name=None, version="1.2.3", source="path",
    lock_hash=True,
):
    run_directory = input_file.parent
    wheel_directory = run_directory / "wheels"
    wheel_directory.mkdir(exist_ok=True)
    module = name.replace("-", "_")
    filename = f"{module}-{version}-py3-none-any.whl"
    wheel = wheel_directory / filename
    metadata = f"{module}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{module}/__init__.py", "FROZEN = True\n")
        archive.writestr(
            f"{metadata}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{metadata}/RECORD", "")
    artifact = {
        "package": name, "version": version,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "wheel": wheel.relative_to(run_directory).as_posix(),
    }
    if name == "scheme":
        lock = run_directory / "environment/uv.lock"
        lock.write_bytes(lock.read_bytes().split(b"[[package]]")[0])
    relative = f"../wheels/{filename}"
    wheel_metadata = {"filename": filename}
    if source in {"path", "path-absolute"}:
        lock_source = {"path": relative if source == "path" else str(wheel)}
    else:
        registry = {
            "registry-path": "../wheels", "registry-url": "../wheels",
            "registry-absolute": str(wheel_directory), "registry-uri": wheel_directory.as_uri(),
            "registry-wheel-uri": "../wheels",
        }[source]
        lock_source = {"registry": registry}
        wheel_metadata = (
            {"url": relative} if source == "registry-url"
            else {"url": wheel.as_uri()} if source == "registry-wheel-uri"
            else {"path": filename}
        )
    if lock_hash:
        wheel_metadata["hash"] = "sha256:" + artifact["sha256"]
    add_lock_package(
        input_file, name=locked_name or name, version=version, source=lock_source,
        wheels=[wheel_metadata],
    )
    return artifact


@pytest.fixture
def local_artifact(input_file):
    return write_local_wheel(input_file)


def mock_task_processes(
    calls, input_file, *, events=None, after_sync=None, after_run=None, returncodes=(0, 0),
):
    project = input_file.parent / "environment"
    input_sha256 = hashlib.sha256(input_file.read_bytes()).hexdigest()
    lock_sha256 = hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest()

    def execute(command, *, cwd, env):
        index = calls.process.call_count - 1
        if events is not None:
            events.append(command[1])
        if returncodes[index]:
            return SimpleNamespace(returncode=returncodes[index])
        if index == 0:
            entry = project / ".venv" / ("Scripts/scheme.exe" if os.name == "nt" else "bin/scheme")
            entry.parent.mkdir(parents=True)
            entry.touch()
            if after_sync is not None:
                after_sync(project)
        else:
            output = input_file.parent / "report"
            output.mkdir()
            report = output / "analysis.json"
            report.write_bytes(b'{"result":1}\n')
            manifest = {
                "protocol": 1, "status": "success", "input_sha256": input_sha256,
                "lock_sha256": lock_sha256, "report_kind": "factor",
                "reports": {"analysis": report.name},
                "report_sha256": {report.name: hashlib.sha256(report.read_bytes()).hexdigest()},
            }
            (output / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
            if after_run is not None:
                after_run(output)
        return SimpleNamespace(returncode=0)

    calls.process.side_effect = execute


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("grandfathered", [False, True])
def test_admission_precedes_unchanged_locked_flow(
    kind, grandfathered, tmp_path, external_calls, monkeypatch,
):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    project = input_file.parent / "environment"
    source = input_file.read_bytes()
    lock_source = (project / "uv.lock").read_bytes()
    response = respond(
        external_calls, json.dumps({"allowed": True, "grandfathered": grandfathered}).encode(),
    )
    events = []

    def admit(request, *, timeout):
        events.append("admission")
        assert not (project / ".venv").exists()
        assert input_file.read_bytes() == source
        assert (project / "uv.lock").read_bytes() == lock_source
        return response

    external_calls.open.side_effect = admit
    mock_task_processes(external_calls, input_file, events=events)
    inherited_keys = (
        "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_DEFAULT_INDEX", "UV_INDEX_URL",
        "UV_INDEX", "UV_EXTRA_INDEX_URL",
    )
    for key in inherited_keys:
        monkeypatch.setenv(key, "worker-setting")
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    monkeypatch.setenv("UV_CACHE_DIR", "/cache/on/another/filesystem")

    assert main(["apps", kind, "--input-file", str(input_file)]) == 0
    assert events == ["admission", "sync", "run"]
    external_calls.open.assert_called_once()
    request = external_calls.open.call_args.args[0]
    assert request.full_url == (
        f"http://backend:8000/api/v1/version-policy/tasks/{input_file.parent.name}/check"
    )
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert json.loads(request.data) == {
        "kind": kind, "input_sha256": hashlib.sha256(source).hexdigest(),
        "lock_sha256": hashlib.sha256(lock_source).hexdigest(),
    }
    assert external_calls.open.call_args.kwargs == {"timeout": 15}
    response.read.assert_called_once_with(64 * 1024 + 1)
    sync, run = external_calls.process.call_args_list
    assert sync.args[0] == [
        "uv", "sync", "--project", str(project), "--locked", "--no-editable", "--no-dev",
    ]
    entry = project / ".venv" / ("Scripts/scheme.exe" if os.name == "nt" else "bin/scheme")
    assert run.args[0] == [
        "uv", "run", "--project", str(project), "--no-sync", str(entry), "run",
        "--input", str(input_file), "--output", str(input_file.parent / "report"),
    ]
    for call in (sync, run):
        assert call.kwargs["cwd"] == project
        assert call.kwargs["env"]["UV_PROJECT_ENVIRONMENT"] == str(project / ".venv")
        assert call.kwargs["env"]["UV_LINK_MODE"] == "hardlink"
        assert call.kwargs["env"]["UV_CACHE_DIR"] == str(input_file.parent.parent / ".uv-cache")
        assert all(key not in call.kwargs["env"] for key in inherited_keys)
    assert all(os.environ[key] == "worker-setting" for key in inherited_keys)
    assert input_file.read_bytes() == source
    assert (project / "uv.lock").read_bytes() == lock_source


def test_backend_url_override(input_file, external_calls, monkeypatch):
    monkeypatch.setenv("SOLO_BACKEND_URL", "https://backend.example:8443/solo/")
    respond(external_calls)
    external_calls.process.side_effect = None
    external_calls.process.return_value = SimpleNamespace(returncode=7)
    assert task.run_task(input_file, kind="factor") == 7
    assert external_calls.open.call_args.args[0].full_url == (
        f"https://backend.example:8443/solo/api/v1/version-policy/tasks/{input_file.parent.name}/check"
    )


@pytest.mark.parametrize("parent", ["project", "123", "nested"])
def test_missing_immediate_parent_uuid_never_guesses_a_run_id(
    parent, tmp_path, external_calls,
):
    directory = tmp_path / "runs" / str(uuid4()) / parent
    input_file = write_run(directory)
    data = json.loads(input_file.read_bytes())
    data["run_id"] = str(uuid4())
    input_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Backend.*UUID"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("format", ["uppercase", "hex"])
def test_parent_uuid_is_canonicalized(format, input_file, external_calls):
    identifier = input_file.parent.name
    name = identifier.upper() if format == "uppercase" else identifier.replace("-", "")
    input_file.parent.rename(input_file.parent.with_name(name))
    input_file = input_file.parent.with_name(name) / "input.json"
    respond(external_calls)
    external_calls.process.side_effect = None
    external_calls.process.return_value = SimpleNamespace(returncode=7)
    assert task.run_task(input_file, kind="factor") == 7
    assert external_calls.open.call_args.args[0].full_url.endswith(f"/tasks/{identifier}/check")


@pytest.mark.parametrize("kind", KINDS)
def test_retired_task_rejected_before_install(kind, tmp_path, external_calls, capsys):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://backend:8000", 422, "Unprocessable Entity", {},
        io.BytesIO(json.dumps({"detail": "Scheme 1.0 已退休，禁止新任务"}).encode()),
    )
    assert main(["apps", kind, "--input-file", str(input_file)]) == 1
    message = capsys.readouterr().err
    assert "HTTP 422" in message
    assert "Scheme 1.0 已退休" in message
    assert_not_started(external_calls, input_file)


def test_explicit_disallow_body_fails_closed(input_file, external_calls):
    respond(external_calls, b'{"allowed":false,"grandfathered":true}')
    with pytest.raises(RuntimeError, match="拒绝任务准入"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("body", [
    b"", b"not-json", b"\xff", b'{"allowed":true', b"null", b"[]", b"true", b"{}",
    b'{"grandfathered":false}', b'{"allowed":true}',
    b'{"allowed":1,"grandfathered":false}', b'{"allowed":"true","grandfathered":false}',
    b'{"allowed":null,"grandfathered":false}', b'{"allowed":true,"grandfathered":null}',
    b'{"allowed":true,"grandfathered":1}', b'{"allowed":true,"grandfathered":"false"}',
    pytest.param(ALLOWED + b" " * (64 * 1024), id="oversized-body"),
])
def test_missing_or_invalid_admission_fails_closed(body, input_file, external_calls):
    respond(external_calls, body)
    with pytest.raises(RuntimeError, match="准入响应无效"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("status", [204, 301, 503])
def test_non_success_http_response_fails_closed(status, input_file, external_calls):
    respond(external_calls, status=status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("error", [
    urllib.error.URLError("http://user:network-secret@backend/?token=network-secret"),
    TimeoutError("password=network-secret"), ConnectionError("token=network-secret"),
    HTTPException("Authorization: Bearer network-secret"),
    ValueError("http://user:network-secret@backend"), UnicodeError("token=network-secret"),
])
def test_network_errors_are_fail_closed_and_sanitized(error, input_file, external_calls):
    external_calls.open.side_effect = error
    with pytest.raises(RuntimeError, match="不可用或请求超时") as caught:
        task.run_task(input_file, kind="factor")
    assert "network-secret" not in "".join(traceback.format_exception(caught.value))
    assert_not_started(external_calls, input_file)


def test_response_read_timeout_fails_closed(input_file, external_calls):
    response = respond(external_calls)
    response.read.side_effect = TimeoutError("token=read-secret")
    with pytest.raises(RuntimeError, match="不可用或请求超时") as caught:
        task.run_task(input_file, kind="factor")
    assert "read-secret" not in "".join(traceback.format_exception(caught.value))
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500, 503])
@pytest.mark.parametrize("nested", [False, True])
def test_http_failures_do_not_expose_credentials(status, nested, input_file, external_calls, capsys):
    reason = (
        "Scheme 1.0 retired\nhttps://user:url-secret@backend/?token=url-secret "
        'password="password-secret words" SOLO_BACKEND_TOKEN=token-secret '
        "Authorization: Bearer auth-secret api_key=key-secret Basic basic-secret\x1b[31m"
    )
    detail = {"code": "version_retired", "reason": reason} if nested else reason
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://user:request-secret@backend/?token=request-secret", status,
        "password=status-secret", {}, io.BytesIO(json.dumps({"detail": detail}).encode()),
    )
    assert main(["apps", "factor", "--input-file", str(input_file)]) == 1
    message = capsys.readouterr().err
    assert f"HTTP {status}" in message
    for secret in (
        "url-secret", "password-secret", "token-secret", "auth-secret", "key-secret",
        "basic-secret", "request-secret", "status-secret",
    ):
        assert secret not in message
    assert "\x1b" not in message
    if status == 422:
        assert "Scheme 1.0 retired" in message
        assert "[redacted" in message
    else:
        assert "Scheme 1.0 retired" not in message
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("body", [
    b"not-json", b"\xff", b"[]",
    b'{"detail":[{"input":{"password":"echo-secret"},"msg":"echo-secret"}]}',
    b'{"detail":{"input":"echo-secret"}}', b'{"detail":{"reason":["echo-secret"]}}',
])
def test_422_invalid_or_validation_body_is_not_echoed(body, input_file, external_calls):
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://backend:8000", 422, "Unprocessable Entity", {}, io.BytesIO(body),
    )
    with pytest.raises(RuntimeError, match="HTTP 422") as caught:
        task.run_task(input_file, kind="factor")
    assert "echo-secret" not in str(caught.value)
    assert_not_started(external_calls, input_file)


def test_422_reason_is_bounded(input_file, external_calls):
    body = io.BytesIO(json.dumps({"detail": "Scheme retired " + "x" * 10000}).encode())
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://backend:8000", 422, "Unprocessable Entity", {}, body,
    )
    with pytest.raises(RuntimeError, match="Scheme retired") as caught:
        task.run_task(input_file, kind="factor")
    assert len(str(caught.value)) < 1100
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("url", [
    "", "http://", "ftp://backend:8000", "file:///tmp/backend",
    "http://user:config-secret@backend:8000", "http://backend:bad", "http://backend:0",
    "http://backend:8000?token=config-secret", "http://backend:8000#config-secret",
    "http://back\nend:8000",
])
def test_invalid_backend_configuration_fails_closed(url, input_file, external_calls, monkeypatch):
    monkeypatch.setenv("SOLO_BACKEND_URL", url)
    with pytest.raises(ValueError, match="SOLO_BACKEND_URL") as caught:
        task.run_task(input_file, kind="factor")
    assert "config-secret" not in str(caught.value)
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("filename", ["input.json", "environment/uv.lock"])
def test_admission_time_artifact_change_fails_before_sync(filename, input_file, external_calls):
    response = respond(external_calls)
    source = input_file.read_bytes()
    lock_source = (input_file.parent / "environment/uv.lock").read_bytes()

    def admit(request, *, timeout):
        assert json.loads(request.data) == {
            "kind": "factor", "input_sha256": hashlib.sha256(source).hexdigest(),
            "lock_sha256": hashlib.sha256(lock_source).hexdigest(),
        }
        changed = input_file.parent / filename
        changed.write_bytes(changed.read_bytes() + b" ")
        return response

    external_calls.open.side_effect = admit
    with pytest.raises(ValueError, match="准入期间.*发生变化"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("source", ["editable", "directory"])
def test_mutable_lock_source_still_fails_before_admission(source, input_file, external_calls):
    lock = input_file.parent / "environment/uv.lock"
    lock.write_text(
        f'[[package]]\nname = "scheme"\nsource = {{ {source} = "." }}\n', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="可变源码目录"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


def test_existing_output_manifest_still_prevents_overwrite(input_file, external_calls):
    output = input_file.parent / "report"
    output.mkdir()
    manifest = output / "run.json"
    manifest.write_bytes(b"existing run")
    with pytest.raises(FileExistsError, match="新的 Run/Attempt"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    external_calls.process.assert_not_called()
    assert manifest.read_bytes() == b"existing run"


@pytest.mark.parametrize("invalid", ["missing-pyproject", "wrong-lock-name"])
def test_lock_project_requirements_still_enforced(invalid, input_file, external_calls):
    project = input_file.parent / "environment"
    if invalid == "missing-pyproject":
        (project / "pyproject.toml").unlink()
    else:
        (project / "uv.lock").rename(project / "other.lock")
        data = json.loads(input_file.read_bytes())
        data["environment"]["lockfile"] = "environment/other.lock"
        input_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="uv.lock.*pyproject.toml"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("returncodes", [(7, 0), (0, 9)])
def test_subprocess_failure_codes_are_unchanged(returncodes, input_file, external_calls):
    respond(external_calls)
    mock_task_processes(external_calls, input_file, returncodes=returncodes)
    assert task.run_task(input_file, kind="factor") == max(returncodes)
    assert external_calls.process.call_count == (1 if returncodes[0] else 2)
    assert not (input_file.parent / "report/run.json").exists()


def test_no_global_scheme_fallback_after_admission(input_file, external_calls):
    respond(external_calls)
    external_calls.process.side_effect = None
    external_calls.process.return_value = SimpleNamespace(returncode=0)
    with pytest.raises(FileNotFoundError, match="scheme 命令入口"):
        task.run_task(input_file, kind="factor")
    assert external_calls.process.call_count == 1


@pytest.mark.parametrize("filename", ["input.json", "environment/uv.lock"])
def test_execution_time_artifact_changes_still_fail(filename, input_file, external_calls):
    respond(external_calls)

    def change_artifact(output):
        changed = input_file.parent / filename
        changed.write_bytes(changed.read_bytes() + b" ")

    mock_task_processes(external_calls, input_file, after_run=change_artifact)
    with pytest.raises(ValueError, match="运行期间.*发生变化"):
        task.run_task(input_file, kind="factor")


@pytest.mark.parametrize("invalid", [
    "protocol", "status", "input-hash", "lock-hash", "reports", "report-hashes",
    "missing-report", "empty-report", "changed-report", "escape-report", "absolute-report",
])
def test_output_checks_still_enforced_after_admission(invalid, input_file, external_calls):
    respond(external_calls)

    def corrupt_output(output):
        manifest_file = output / "run.json"
        manifest = json.loads(manifest_file.read_bytes())
        report = output / "analysis.json"
        if invalid == "protocol":
            manifest["protocol"] = 2
        elif invalid == "status":
            manifest["status"] = "failed"
        elif invalid == "input-hash":
            manifest["input_sha256"] = "0" * 64
        elif invalid == "lock-hash":
            manifest["lock_sha256"] = "0" * 64
        elif invalid == "reports":
            manifest["reports"] = {}
        elif invalid == "report-hashes":
            manifest["report_sha256"] = None
        elif invalid == "missing-report":
            report.unlink()
        elif invalid == "empty-report":
            report.write_bytes(b"")
        elif invalid == "changed-report":
            report.write_bytes(b"changed")
        elif invalid == "escape-report":
            manifest["reports"] = {"analysis": "../outside.json"}
        elif invalid == "absolute-report":
            manifest["reports"] = {"analysis": str(report.resolve())}
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    mock_task_processes(external_calls, input_file, after_run=corrupt_output)
    with pytest.raises(ValueError):
        task.run_task(input_file, kind="factor")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("grandfathered", [False, True])
def test_frozen_research_wheel_uses_accepted_snapshot_without_source_projects(
    kind, grandfathered, tmp_path, external_calls, monkeypatch,
):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    artifact = write_local_wheel(input_file, name=f"{kind}-abcd")
    source_project = tmp_path / "source-projects" / artifact["package"]
    source_project.mkdir(parents=True)
    (source_project / "pyproject.toml").write_text("source project is not a runtime dependency")
    shutil.rmtree(source_project.parent)
    before = input_file.read_bytes(), (input_file.parent / "environment/uv.lock").read_bytes()
    response = respond(external_calls, artifact_response([artifact], grandfathered=grandfathered))
    events = []

    def admit(request, *, timeout):
        events.append("admission")
        return response

    original_import = builtins.__import__

    def no_scheme_import(name, *args, **kwargs):
        if name == "scheme" or name.startswith("scheme."):
            raise AssertionError("Runtime must not import Scheme")
        return original_import(name, *args, **kwargs)

    external_calls.open.side_effect = admit
    monkeypatch.setattr(builtins, "__import__", no_scheme_import)
    hashes = []
    original_hash = task.file_hash

    def observe_hash(path):
        if path == input_file.parent / artifact["wheel"]:
            hashes.append(tuple(events))
        return original_hash(path)

    monkeypatch.setattr(task, "file_hash", observe_hash)
    mock_task_processes(external_calls, input_file, events=events)
    assert task.run_task(input_file, kind=kind) == 0
    assert events == ["admission", "sync", "run"]
    assert hashes == [("admission",), ("admission", "sync"), ("admission", "sync", "run")]
    external_calls.open.assert_called_once()
    assert input_file.read_bytes() == before[0]
    assert (input_file.parent / "environment/uv.lock").read_bytes() == before[1]
    assert not source_project.exists()


@pytest.mark.parametrize("source", [
    "path", "path-absolute", "registry-path", "registry-url", "registry-absolute",
    "registry-uri", "registry-wheel-uri",
])
@pytest.mark.parametrize("lock_hash", [False, True])
def test_local_lock_source_forms_match_origin_free_snapshot(
    source, lock_hash, input_file, external_calls,
):
    artifact = write_local_wheel(input_file, source=source, lock_hash=lock_hash)
    respond(external_calls, artifact_response([artifact]))
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0
    external_calls.open.assert_called_once()
    assert external_calls.process.call_count == 2


@pytest.mark.parametrize("name", ["scheme", "helper-lib"])
def test_local_nonresearch_and_scheme_wheels_require_artifacts(name, input_file, external_calls):
    artifact = write_local_wheel(input_file, name=name, source="registry-path")
    respond(external_calls, artifact_response([artifact]))
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0


def test_every_local_wheel_must_be_in_exact_snapshot(input_file, external_calls):
    artifacts = [
        write_local_wheel(input_file, name="scheme", source="registry-path"),
        write_local_wheel(input_file),
        write_local_wheel(input_file, name="helper-lib", source="registry-path"),
    ]
    respond(external_calls, artifact_response(list(reversed(artifacts))))
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0
    external_calls.open.assert_called_once()


@pytest.mark.parametrize("body", [ALLOWED, artifact_response([])])
@pytest.mark.parametrize("name", ["factor-abcd", "helper-lib", "scheme"])
def test_local_lock_missing_snapshot_denies_before_sync(body, name, input_file, external_calls):
    write_local_wheel(input_file, name=name)
    respond(external_calls, body)
    with pytest.raises(ValueError, match="接受快照缺失"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_called_once()
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("body", [ALLOWED, artifact_response([])])
def test_pure_registry_lock_accepts_legacy_or_empty_manifest(body, input_file, external_calls):
    respond(external_calls, body)
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0
    external_calls.open.assert_called_once()


@pytest.mark.parametrize("kind", KINDS)
def test_research_registry_package_cannot_bypass_local_snapshot(kind, input_file, external_calls):
    add_lock_package(
        input_file, name=f"{kind}-abcd", version="1.2.3",
        source={"registry": "https://pypi.org/simple"},
    )
    respond(external_calls, artifact_response([]))
    with pytest.raises(ValueError, match="研究包必须"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("phase", ["before-admission", "admission", "sync", "run"])
@pytest.mark.parametrize("mutation", ["tamper", "remove"])
def test_local_wheel_tamper_is_detected_at_each_boundary(
    phase, mutation, local_artifact, input_file, external_calls,
):
    wheel = input_file.parent / local_artifact["wheel"]
    response = respond(external_calls, artifact_response([local_artifact]))

    def change(*args):
        if mutation == "tamper":
            wheel.write_bytes(wheel.read_bytes() + b"changed after accepted snapshot")
        else:
            wheel.unlink()

    if phase == "before-admission":
        change()
    elif phase == "admission":
        def admit(request, *, timeout):
            change()
            return response
        external_calls.open.side_effect = admit
    mock_task_processes(
        external_calls, input_file,
        after_sync=change if phase == "sync" else None,
        after_run=change if phase == "run" else None,
    )
    with pytest.raises(ValueError, match="wheel"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_called_once()
    assert external_calls.process.call_count == {"before-admission": 0, "admission": 0, "sync": 1, "run": 2}[phase]
    if phase in {"before-admission", "admission"}:
        assert_not_started(external_calls, input_file)
    if phase == "sync":
        assert not (input_file.parent / "report").exists()


@pytest.mark.parametrize("filename", ["input.json", "environment/uv.lock"])
def test_sync_time_input_or_lock_change_prevents_execution(filename, input_file, external_calls):
    respond(external_calls)

    def change(project):
        path = input_file.parent / filename
        path.write_bytes(path.read_bytes() + b" ")

    mock_task_processes(external_calls, input_file, after_sync=change)
    with pytest.raises(ValueError, match="同步期间.*发生变化"):
        task.run_task(input_file, kind="factor")
    assert external_calls.process.call_count == 1
    assert not (input_file.parent / "report").exists()


@pytest.mark.parametrize("value", [None, False, 1, "", {}, [None], [1], [[]], ["wheel"]])
def test_artifact_manifest_types_are_strict(value, input_file, external_calls):
    respond(external_calls, artifact_response(value))
    with pytest.raises(RuntimeError, match="准入响应无效"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("invalid", [
    "missing-package", "missing-version", "missing-sha256", "missing-wheel", "extra-field",
    "name-upper", "name-underscore", "name-dot", "name-repeated-hyphen", "name-empty",
    "name-leading-hyphen", "name-long", "name-type", "version-empty", "version-type",
    "version-long", "version-control", "hash-short", "hash-nonhex", "hash-prefix", "hash-type",
    "path-absolute", "path-drive", "path-unc", "path-escape", "path-nested-escape",
    "path-dot", "path-double-slash", "path-backslash", "path-url", "path-control",
    "path-suffix", "path-long", "path-empty", "path-type", "path-ads", "path-trailing-dot",
    "path-trailing-space", "duplicate-name", "duplicate-path", "too-many",
])
def test_invalid_or_unbounded_artifact_snapshot_never_starts(
    invalid, local_artifact, input_file, external_calls,
):
    artifact = dict(local_artifact)
    manifest = [artifact]
    if invalid.startswith("missing-"):
        del artifact[invalid.removeprefix("missing-")]
    elif invalid == "extra-field":
        artifact["origin"] = "not part of the runtime identity"
    elif invalid == "duplicate-name":
        manifest.append(dict(artifact, version="2.0", wheel="wheels/other-2.0-py3-none-any.whl"))
    elif invalid == "duplicate-path":
        manifest.append(dict(artifact, package="other-package"))
    elif invalid == "too-many":
        manifest = [dict(artifact, package=f"pkg-{index}", wheel=f"wheels/{index}.whl") for index in range(257)]
        assert len(artifact_response(manifest)) < 64 * 1024
    else:
        field, value = {
            "name-upper": ("package", "Factor-abcd"),
            "name-underscore": ("package", "factor_abcd"),
            "name-dot": ("package", "factor.abcd"),
            "name-repeated-hyphen": ("package", "factor--abcd"),
            "name-empty": ("package", ""), "name-leading-hyphen": ("package", "-factor-abcd"),
            "name-long": ("package", "x" * 201), "name-type": ("package", 1),
            "version-empty": ("version", ""), "version-type": ("version", 1),
            "version-long": ("version", "1" * 129), "version-control": ("version", "1.0\n"),
            "hash-short": ("sha256", "a" * 63), "hash-nonhex": ("sha256", "g" * 64),
            "hash-prefix": ("sha256", "sha256:" + "a" * 64), "hash-type": ("sha256", 1),
            "path-absolute": ("wheel", "/wheels/factor.whl"),
            "path-drive": ("wheel", "C:/wheels/factor.whl"),
            "path-unc": ("wheel", "//server/wheels/factor.whl"),
            "path-escape": ("wheel", "../factor.whl"),
            "path-nested-escape": ("wheel", "wheels/../factor.whl"),
            "path-dot": ("wheel", "./wheels/factor.whl"),
            "path-double-slash": ("wheel", "wheels//factor.whl"),
            "path-backslash": ("wheel", "wheels\\factor.whl"),
            "path-url": ("wheel", "file:///wheels/factor.whl"),
            "path-control": ("wheel", "wheels/\x00factor.whl"),
            "path-suffix": ("wheel", "wheels/factor.zip"),
            "path-long": ("wheel", "wheels/" + "x" * 1024 + ".whl"),
            "path-empty": ("wheel", ""), "path-type": ("wheel", 1),
            "path-ads": ("wheel", "wheels/factor:payload.whl"),
            "path-trailing-dot": ("wheel", "wheels./factor.whl"),
            "path-trailing-space": ("wheel", "wheels /factor.whl"),
        }[invalid]
        artifact[field] = value
    respond(external_calls, artifact_response(manifest))
    with pytest.raises(RuntimeError, match="准入响应无效"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("invalid", ["package", "version", "digest", "path", "extra-artifact"])
def test_well_formed_snapshot_conflicts_with_lock_are_denied(
    invalid, local_artifact, input_file, external_calls,
):
    artifact = dict(local_artifact)
    if invalid == "package":
        artifact["package"] = "unaccepted-package"
    elif invalid == "version":
        artifact["version"] = "9.9.9"
    elif invalid == "digest":
        artifact["sha256"] = "0" * 64
    elif invalid == "path":
        other = input_file.parent / "wheels/other.whl"
        other.write_bytes((input_file.parent / artifact["wheel"]).read_bytes())
        artifact["wheel"] = other.relative_to(input_file.parent).as_posix()
    manifest = [artifact]
    if invalid == "extra-artifact":
        manifest.append(dict(artifact, package="unaccepted-package", wheel="wheels/extra.whl"))
    respond(external_calls, artifact_response(manifest))
    with pytest.raises(ValueError, match="接受快照"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("conflict", ["local-alias", "remote-alias", "shared-path"])
def test_lock_local_package_identities_and_paths_are_unique(
    conflict, input_file, external_calls,
):
    artifact = write_local_wheel(input_file, name="helper-lib")
    source = (
        {"registry": "https://pypi.org/simple"} if conflict == "remote-alias"
        else {"path": "../" + artifact["wheel"]}
    )
    add_lock_package(
        input_file, name="other-lib" if conflict == "shared-path" else "Helper_Lib",
        version="1.2.3", source=source,
    )
    respond(external_calls, artifact_response([artifact]))
    with pytest.raises(ValueError, match="身份.*冲突|路径冲突"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("phase", ["admission", "sync", "run"])
def test_local_wheel_symlink_escape_fails_at_every_boundary(
    phase, local_artifact, input_file, external_calls, tmp_path,
):
    wheel = input_file.parent / local_artifact["wheel"]
    outside = tmp_path / "other-run" / wheel.name
    outside.parent.mkdir()
    outside.write_bytes(wheel.read_bytes())
    probe = input_file.parent / "symlink-probe"
    try:
        probe.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("File symlinks are unavailable on this host")
    probe.unlink()

    def escape(*args):
        wheel.unlink()
        wheel.symlink_to(outside)

    response = respond(external_calls, artifact_response([local_artifact]))
    if phase == "admission":
        def admit(request, *, timeout):
            escape()
            return response
        external_calls.open.side_effect = admit
    mock_task_processes(
        external_calls, input_file,
        after_sync=escape if phase == "sync" else None,
        after_run=escape if phase == "run" else None,
    )
    with pytest.raises(ValueError, match="符号链接必须位于"):
        task.run_task(input_file, kind="factor")
    assert external_calls.process.call_count == {"admission": 0, "sync": 1, "run": 2}[phase]


@pytest.mark.parametrize("source", ["path", "registry"])
def test_lock_cannot_read_other_run_wheels_even_with_matching_digest(
    source, input_file, external_calls, tmp_path,
):
    other_input = write_run(tmp_path / "runs" / str(uuid4()))
    artifact = write_local_wheel(other_input)
    wheel = other_input.parent / artifact["wheel"]
    add_lock_package(
        input_file, name=artifact["package"], version=artifact["version"],
        source={source: str(wheel if source == "path" else wheel.parent)},
        wheels=[{"path": wheel.name, "hash": "sha256:" + artifact["sha256"]}],
    )
    respond(external_calls, artifact_response([artifact]))
    with pytest.raises(ValueError, match="必须位于本次 Run 内"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("body", [
    b'{"allowed":true,"allowed":false,"grandfathered":false}',
    b'{"allowed":true,"grandfathered":false,"artifacts":[],"artifacts":[]}',
    b'{"allowed":true,"grandfathered":false,"artifacts":[{"package":"a","package":"b"}]}',
    b'{"allowed":true,"grandfathered":false,"artifacts":' + b"[" * 2000 + b"]" * 2000 + b"}",
])
def test_ambiguous_or_deep_json_admission_is_denied(body, input_file, external_calls):
    respond(external_calls, body)
    with pytest.raises(RuntimeError, match="准入响应无效"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


def test_check_task_admission_returns_validated_manifest(local_artifact, input_file, external_calls):
    respond(external_calls, artifact_response([local_artifact]))
    assert task._check_task_admission(
        uuid4(), kind="factor", input_sha256="1" * 64, lock_sha256="2" * 64,
    ) == [local_artifact]
    external_calls.open.assert_called_once()


def test_hex_digest_case_and_canonical_lock_name_match(input_file, external_calls):
    artifact = write_local_wheel(input_file, locked_name="Factor_abcd")
    respond(external_calls, artifact_response([dict(artifact, sha256=artifact["sha256"].upper())]))
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0


@pytest.mark.parametrize("invalid", [
    "no-wheels", "multiple-wheels", "ambiguous-path-url", "remote-wheel", "missing-reference",
    "bad-hash", "wrong-hash",
])
def test_invalid_flat_registry_wheel_lock_fails_closed(invalid, input_file, external_calls):
    artifact = write_local_wheel(input_file, name="helper-lib")
    lock = input_file.parent / "environment/uv.lock"
    lock.write_bytes(lock.read_bytes().rsplit(b"[[package]]", 1)[0])
    wheel = input_file.parent / artifact["wheel"]
    metadata = {"path": wheel.name, "hash": "sha256:" + artifact["sha256"]}
    wheels = [metadata]
    if invalid == "no-wheels":
        wheels = []
    elif invalid == "multiple-wheels":
        wheels.append(dict(metadata, path="other.whl"))
    elif invalid == "ambiguous-path-url":
        metadata["url"] = "../" + artifact["wheel"]
    elif invalid == "remote-wheel":
        metadata = {"url": "https://registry.example/other.whl"}
        wheels = [metadata]
    elif invalid == "missing-reference":
        del metadata["path"]
    elif invalid == "bad-hash":
        metadata["hash"] = "md5:0123"
    elif invalid == "wrong-hash":
        metadata["hash"] = "sha256:" + "0" * 64
    add_lock_package(
        input_file, name=artifact["package"], version=artifact["version"],
        source={"registry": "../wheels"}, wheels=wheels,
    )
    respond(external_calls, artifact_response([artifact]))
    with pytest.raises(ValueError):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


def test_source_path_without_wheels_uses_accepted_digest(input_file, external_calls):
    artifact = write_local_wheel(input_file)
    lock = input_file.parent / "environment/uv.lock"
    lock.write_bytes(lock.read_bytes().rsplit(b"wheels =", 1)[0])
    respond(external_calls, artifact_response([artifact]))
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind="factor") == 0


@pytest.mark.parametrize("source", ["file-uri", "relative"])
def test_unsupported_local_url_cannot_bypass_manifest(source, input_file, external_calls):
    artifact = write_local_wheel(input_file, name="helper-lib")
    lock = input_file.parent / "environment/uv.lock"
    lock.write_bytes(lock.read_bytes().rsplit(b"[[package]]", 1)[0])
    wheel = input_file.parent / artifact["wheel"]
    add_lock_package(
        input_file, name="helper-lib", version=artifact["version"],
        source={"url": wheel.as_uri() if source == "file-uri" else "../" + artifact["wheel"]},
    )
    respond(external_calls, ALLOWED)
    with pytest.raises(ValueError, match="本地 wheel"):
        task.run_task(input_file, kind="factor")
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("phase", ["admission", "sync", "run"])
def test_symlink_escape_validation_without_host_symlink_privileges(
    phase, local_artifact, input_file, external_calls, monkeypatch, tmp_path,
):
    wheel = input_file.parent / local_artifact["wheel"]
    outside = tmp_path / "outside" / wheel.name
    resolving_escape = False
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if resolving_escape and path == wheel:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    def escape(*args):
        nonlocal resolving_escape
        resolving_escape = True

    response = respond(external_calls, artifact_response([local_artifact]))
    if phase == "admission":
        def admit(request, *, timeout):
            escape()
            return response
        external_calls.open.side_effect = admit
    mock_task_processes(
        external_calls, input_file,
        after_sync=escape if phase == "sync" else None,
        after_run=escape if phase == "run" else None,
    )
    with pytest.raises(ValueError, match="符号链接必须位于"):
        task.run_task(input_file, kind="factor")
    assert external_calls.process.call_count == {"admission": 0, "sync": 1, "run": 2}[phase]
