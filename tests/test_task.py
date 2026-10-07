import hashlib
import io
import json
import os
import traceback
import urllib.error
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


def mock_task_processes(calls, input_file, *, events=None, after_run=None, returncodes=(0, 0)):
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
