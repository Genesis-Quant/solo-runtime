import builtins
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
ALLOWED = b'{"allowed":true}'


def write_run(directory: Path, *, kind: str = "factor") -> Path:
    project = directory / "environment"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "locked-task"\nversion = "1.0.0"\n', encoding="utf-8",
    )
    (project / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "scheme"\nversion = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    input_file = directory / "input.json"
    input_file.write_text(json.dumps({
        "kind": kind, "environment": {"lockfile": "environment/uv.lock"},
        "output": "report", "research": {"note": "研究字段交给 Scheme"},
    }, ensure_ascii=False), encoding="utf-8")
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
            report.write_text('{"result":1}\n', encoding="utf-8")
            manifest = {
                "protocol": 1, "status": "success", "report_kind": "factor",
                "reports": {"analysis": report.name},
            }
            (output / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
            if after_run is not None:
                after_run(output)
        return SimpleNamespace(returncode=0)

    calls.process.side_effect = execute


@pytest.mark.parametrize("kind", KINDS)
def test_admission_precedes_locked_flow(kind, tmp_path, external_calls, monkeypatch):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    project = input_file.parent / "environment"
    response = respond(external_calls)
    events = []

    def admit(request, *, timeout):
        events.append("admission")
        assert not (project / ".venv").exists()
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
    assert json.loads(request.data) == {"kind": kind}
    assert external_calls.open.call_args.kwargs == {"timeout": 15}
    response.read.assert_called_once_with()
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


def test_backend_url_override(input_file, external_calls, monkeypatch):
    monkeypatch.setenv("SOLO_BACKEND_URL", "https://backend.example:8443/solo/")
    respond(external_calls)
    mock_task_processes(external_calls, input_file, returncodes=(7, 0))
    assert task.run_task(input_file, kind="factor") == 7
    assert external_calls.open.call_args.args[0].full_url == (
        f"https://backend.example:8443/solo/api/v1/version-policy/tasks/{input_file.parent.name}/check"
    )


@pytest.mark.parametrize("parent", ["project", "123", "nested"])
def test_missing_immediate_parent_uuid_never_guesses_a_run_id(parent, tmp_path, external_calls):
    input_file = write_run(tmp_path / "runs" / str(uuid4()) / parent)
    with pytest.raises(task.TaskInputError, match="Backend.*UUID"):
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
    mock_task_processes(external_calls, input_file, returncodes=(7, 0))
    assert task.run_task(input_file, kind="factor") == 7
    assert external_calls.open.call_args.args[0].full_url.endswith(f"/tasks/{identifier}/check")


@pytest.mark.parametrize("kind", KINDS)
def test_retired_task_rejected_before_install(kind, tmp_path, external_calls):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://backend:8000", 422, "Unprocessable Entity", {},
        io.BytesIO(json.dumps({"detail": {
            "code": "version_retired", "reason": "Scheme 1.0 已退休，禁止新任务",
        }}).encode()),
    )
    with pytest.raises(RuntimeError, match="HTTP 422.*Scheme 1.0 已退休"):
        task.run_task(input_file, kind=kind)
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("body", [
    b"", b"not-json", b"\xff", b'{"allowed":true', b"null", b"[]", b"true", b"{}",
    b'{"allowed":false}', b'{"allowed":1}', b'{"allowed":"true"}', b'{"allowed":null}',
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
def test_http_failures_do_not_expose_credentials(status, input_file, external_calls):
    reason = (
        "Scheme 1.0 retired\nhttps://user:url-secret@backend/?token=url-secret "
        'password="password-secret words" SOLO_BACKEND_TOKEN=token-secret '
        "Authorization: Bearer auth-secret api_key=key-secret Basic basic-secret\x1b[31m"
    )
    external_calls.open.side_effect = urllib.error.HTTPError(
        "http://user:request-secret@backend/?token=request-secret", status,
        "password=status-secret", {}, io.BytesIO(json.dumps({"detail": {
            "code": "version_retired", "reason": reason,
        }}).encode()),
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as caught:
        task.run_task(input_file, kind="factor")
    message = "".join(traceback.format_exception(caught.value))
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


@pytest.mark.parametrize("source", ["editable", "directory"])
def test_mutable_lock_source_fails_before_admission(source, input_file, external_calls):
    lock = input_file.parent / "environment/uv.lock"
    lock.write_text(
        f'[[package]]\nname = "scheme"\nsource = {{ {source} = "." }}\n', encoding="utf-8",
    )
    with pytest.raises(task.TaskInputError, match="可变源码目录"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


def test_existing_output_manifest_prevents_overwrite(input_file, external_calls):
    output = input_file.parent / "report"
    output.mkdir()
    (output / "run.json").write_text("existing run", encoding="utf-8")
    with pytest.raises(task.TaskInputError, match="新的 Run/Attempt"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    external_calls.process.assert_not_called()


@pytest.mark.parametrize("invalid", ["missing-pyproject", "wrong-lock-name"])
def test_lock_project_requirements(invalid, input_file, external_calls):
    project = input_file.parent / "environment"
    if invalid == "missing-pyproject":
        (project / "pyproject.toml").unlink()
    else:
        (project / "uv.lock").rename(project / "other.lock")
        data = json.loads(input_file.read_text(encoding="utf-8"))
        data["environment"]["lockfile"] = "environment/other.lock"
        input_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(task.TaskInputError, match="uv.lock.*pyproject.toml"):
        task.run_task(input_file, kind="factor")
    external_calls.open.assert_not_called()
    assert_not_started(external_calls, input_file)


@pytest.mark.parametrize("returncodes", [(7, 0), (0, 9)])
def test_subprocess_failure_codes_are_preserved(returncodes, input_file, external_calls):
    respond(external_calls)
    mock_task_processes(external_calls, input_file, returncodes=returncodes)
    assert task.run_task(input_file, kind="factor") == max(returncodes)
    assert external_calls.process.call_count == (1 if returncodes[0] else 2)
    assert not (input_file.parent / "report/run.json").exists()


def test_missing_scheme_entry_is_not_replaced_by_global_install(input_file, external_calls):
    respond(external_calls)
    external_calls.process.side_effect = None
    external_calls.process.return_value = SimpleNamespace(returncode=0)
    with pytest.raises(FileNotFoundError, match="scheme 命令入口"):
        task.run_task(input_file, kind="factor")
    assert external_calls.process.call_count == 1


@pytest.mark.parametrize("invalid", ["protocol", "status", "missing-report", "directory-report"])
def test_output_protocol_status_and_files_are_checked(invalid, input_file, external_calls):
    respond(external_calls)

    def corrupt_output(output):
        manifest_file = output / "run.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if invalid == "protocol":
            manifest["protocol"] = 2
        elif invalid == "status":
            manifest["status"] = "failed"
        else:
            report = output / "analysis.json"
            report.unlink()
            if invalid == "directory-report":
                report.mkdir()
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    mock_task_processes(external_calls, input_file, after_run=corrupt_output)
    with pytest.raises(ValueError):
        task.run_task(input_file, kind="factor")


@pytest.mark.parametrize("reports", [{}, None, [], ["analysis.json"], "analysis.json", True, 1])
def test_output_requires_nonempty_report_mapping(reports, input_file, external_calls):
    respond(external_calls)

    def corrupt_output(output):
        manifest_file = output / "run.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["reports"] = reports
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    mock_task_processes(external_calls, input_file, after_run=corrupt_output)
    with pytest.raises(ValueError, match="reports.*非空映射"):
        task.run_task(input_file, kind="factor")


@pytest.mark.parametrize("relative", [True, False])
def test_reports_cannot_reference_existing_files_outside_output(relative, input_file, external_calls):
    respond(external_calls)
    outside = input_file.parent / "report-other" / "analysis.json"
    outside.parent.mkdir()
    outside.write_text("existing report", encoding="utf-8")
    filename = f"../{outside.parent.name}/{outside.name}" if relative else str(outside)

    def corrupt_output(output):
        manifest_file = output / "run.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["reports"]["outside"] = filename
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    mock_task_processes(external_calls, input_file, after_run=corrupt_output)
    with pytest.raises(ValueError, match="不在输出目录内"):
        task.run_task(input_file, kind="factor")
    assert outside.read_text(encoding="utf-8") == "existing report"


def test_reports_check_resolved_destination(input_file, external_calls, monkeypatch):
    respond(external_calls)
    outside = input_file.parent / "outside.json"
    outside.write_text("existing report", encoding="utf-8")
    resolve = Path.resolve
    report = input_file.parent / "report" / "analysis.json"
    monkeypatch.setattr(Path, "resolve", lambda path: outside if path == report else resolve(path))
    mock_task_processes(external_calls, input_file)
    with pytest.raises(ValueError, match="不在输出目录内"):
        task.run_task(input_file, kind="factor")


@pytest.mark.parametrize("filename", ["nested/analysis.json", "nested/../analysis.json"])
def test_reports_accept_resolved_paths_inside_output(filename, input_file, external_calls):
    respond(external_calls)

    def nested_report(output):
        (output / "nested").mkdir()
        (output / filename).write_text("nested report", encoding="utf-8")
        manifest_file = output / "run.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["reports"]["analysis"] = filename
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    mock_task_processes(external_calls, input_file, after_run=nested_report)
    assert task.run_task(input_file, kind="factor") == 0


def test_declared_report_only_needs_to_exist(input_file, external_calls):
    respond(external_calls)
    mock_task_processes(
        external_calls, input_file,
        after_run=lambda output: (output / "analysis.json").write_bytes(b""),
    )
    assert task.run_task(input_file, kind="factor") == 0


@pytest.mark.parametrize("kind", KINDS)
def test_locked_local_wheel_does_not_import_scheme(kind, tmp_path, external_calls, monkeypatch):
    input_file = write_run(tmp_path / "runs" / str(uuid4()), kind=kind)
    wheel = input_file.parent / f"{kind}_abcd-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel installation belongs to uv")
    with (input_file.parent / "environment/uv.lock").open("a", encoding="utf-8") as lock:
        lock.write(
            f'\n[[package]]\nname = "{kind}-abcd"\nversion = "1.0.0"\n'
            f'source = {{ path = "../{wheel.name}" }}\n',
        )
    original_import = builtins.__import__

    def no_scheme_import(name, *args, **kwargs):
        if name == "scheme" or name.startswith("scheme."):
            raise AssertionError("Runtime must not import Scheme")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_scheme_import)
    respond(external_calls)
    mock_task_processes(external_calls, input_file)
    assert task.run_task(input_file, kind=kind) == 0
