from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from urllib.parse import urlsplit


DEFAULT_QWEN_ENV_FILE = "~/.config/qwen/api.env"
DEFAULT_GLM_ENV_FILE = "~/.config/glm/api.env"
DEFAULT_QWEN_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_GLM_BASE_URL = "https://api.z.ai/api/anthropic"
HTTP_TIMEOUT_SECONDS = 90.0
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class AdapterFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: float = HTTP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def expand_home(path_text: str, environ: Mapping[str, str]) -> Path:
    if path_text.startswith("~/"):
        home = environ.get("HOME") or str(Path.home())
        return Path(home) / path_text[2:]
    return Path(path_text)


def pgl_home(environ: Mapping[str, str]) -> Path:
    configured = environ.get("PGL_HOME")
    if configured:
        return Path(configured)
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".persona-growth-loop"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def resolve_env_file_path(default_text: str, override_name: str, environ: Mapping[str, str]) -> Path:
    chosen = environ.get(override_name) or default_text
    return expand_home(chosen, environ)


def load_required_env_file(path: Path) -> dict[str, str]:
    if not NOFOLLOW:
        raise AdapterFailure("secure env file open is unavailable on this platform")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AdapterFailure(f"required env file missing: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            if not path.exists():
                raise AdapterFailure(f"required env file missing: {path}") from exc
            raise AdapterFailure(f"env file is not a regular file: {path}") from exc
        raise AdapterFailure(f"cannot read env file: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AdapterFailure(f"env file is not a regular file: {path}")
        stream = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = None
        with stream:
            lines = stream.read().splitlines()
    except AdapterFailure:
        raise
    except OSError as exc:
        raise AdapterFailure(f"cannot read env file: {path}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    values: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        name, sep, raw_value = text.partition("=")
        if not sep:
            raise AdapterFailure(f"invalid env assignment at {path}:{number}")
        key = name.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if not key or not value:
            raise AdapterFailure(f"invalid env assignment at {path}:{number}")
        values[key] = value
    return values


def resolve_secret_from_file(
    names: Sequence[str], env_file_path: Path
) -> str:
    file_values = load_required_env_file(env_file_path)
    for name in names:
        value = file_values.get(name)
        if isinstance(value, str) and value:
            return value
    raise AdapterFailure(f"missing required secret in {env_file_path}: {names[0]}")


def resolve_optional_base_url(override_name: str, default: str, environ: Mapping[str, str]) -> str:
    value = environ.get(override_name)
    if isinstance(value, str) and value:
        return value.rstrip("/")
    return default.rstrip("/")


def validate_input_payload(stdin: TextIO) -> dict[str, Any]:
    try:
        payload = json.loads(stdin.read())
    except json.JSONDecodeError as exc:
        raise AdapterFailure("stdin payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AdapterFailure("stdin payload must be a JSON object")
    return payload


def write_output(stdout: TextIO, payload: Mapping[str, object]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    stdout.write("\n")
    stdout.flush()


def append_audit_event(environ: Mapping[str, str], adapter_name: str, event: Mapping[str, object]) -> None:
    try:
        root = pgl_home(environ) / "adapter-log"
        ensure_private_dir(root)
        path = root / f"{adapter_name}.jsonl"
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "a", encoding="utf-8")
            fd = None
            with stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            return
    except Exception:
        return


def default_transport(request: HttpRequest) -> HttpResponse:
    prepared = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(prepared, timeout=request.timeout_seconds) as response:
            return HttpResponse(
                status=int(response.status),
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        raise AdapterFailure(f"http request failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AdapterFailure(f"http request failed: {exc.reason}") from exc
    except OSError as exc:
        raise AdapterFailure(f"http request failed: {exc}") from exc


def post_json(
    url: str,
    body: Mapping[str, object],
    *,
    headers: Mapping[str, str],
    transport: Transport,
) -> tuple[dict[str, Any], dict[str, object]]:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = HttpRequest(
        method="POST",
        url=url,
        headers={"content-type": "application/json", **dict(headers)},
        body=encoded,
    )
    started = time.monotonic()
    response = transport(request)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status < 200 or response.status >= 300:
        raise AdapterFailure(f"http request failed with status {response.status}")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterFailure("http response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AdapterFailure("http response must be a JSON object")
    meta = {
        "http_status": response.status,
        "latency_ms": elapsed_ms,
        "request_id": response.headers.get("x-request-id") or response.headers.get("request-id"),
    }
    return value, meta


def fresh_fence(parts: Sequence[str]) -> str:
    for _ in range(16):
        fence = secrets.token_hex(8)
        if all(fence not in part for part in parts):
            return fence
    raise AdapterFailure("could not allocate a fresh fence token")


def extract_first_json_object(text: str) -> dict[str, Any]:
    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != "{":
        raise AdapterFailure("model output must begin with a JSON object")
    depth = 0
    in_string = False
    escaped = False
    end_index = None
    for position in range(index, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end_index = position + 1
                break
    if end_index is None:
        raise AdapterFailure("model output JSON object is unterminated")
    trailer = text[end_index:]
    if trailer.strip():
        raise AdapterFailure("model output must end at the first balanced JSON object")
    try:
        value = json.loads(text[index:end_index])
    except json.JSONDecodeError as exc:
        raise AdapterFailure("model output JSON object is invalid") from exc
    if not isinstance(value, dict):
        raise AdapterFailure("model output JSON object must decode to an object")
    return value


def decode_block_files(
    block: object,
    *,
    decoded_limit_bytes: int,
) -> tuple[list[dict[str, object]], int]:
    if not isinstance(block, dict) or set(block) != {"files"}:
        raise AdapterFailure("block schema mismatch")
    files = block.get("files")
    if not isinstance(files, list) or not files:
        raise AdapterFailure("block files must be a non-empty array")
    decoded_files: list[dict[str, object]] = []
    total_bytes = 0
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "bytes_b64", "sha256"}:
            raise AdapterFailure(f"block file schema mismatch at index {index}")
        path = item.get("path")
        bytes_b64 = item.get("bytes_b64")
        expected_sha = item.get("sha256")
        if not isinstance(path, str) or not path or not isinstance(bytes_b64, str) or not isinstance(expected_sha, str):
            raise AdapterFailure(f"block file fields invalid at index {index}")
        try:
            raw = base64.b64decode(bytes_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise AdapterFailure(f"block file base64 invalid at index {index}") from exc
        actual_sha = sha256_bytes(raw)
        if actual_sha != expected_sha:
            raise AdapterFailure(f"block file sha256 mismatch at index {index}")
        total_bytes += len(raw)
        if total_bytes > decoded_limit_bytes:
            raise AdapterFailure(f"decoded block exceeds {decoded_limit_bytes} bytes")
        decoded_files.append(
            {
                "path": path,
                "sha256": actual_sha,
                "size_bytes": len(raw),
                "text": raw.decode("utf-8", errors="replace"),
            }
        )
    return decoded_files, total_bytes


def openai_message_text(response: Mapping[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AdapterFailure("openai response choices are missing")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise AdapterFailure("openai response choice is invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AdapterFailure("openai response message is invalid")
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        text = "".join(parts)
        if text:
            return text
    raise AdapterFailure("openai response text is missing")


def anthropic_message_text(response: Mapping[str, object]) -> str:
    content = response.get("content")
    if isinstance(content, str) and content:
        return content
    if not isinstance(content, list) or not content:
        raise AdapterFailure("anthropic response content is missing")
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    text = "".join(parts)
    if not text:
        raise AdapterFailure("anthropic response text is missing")
    return text


def usage_summary(response: Mapping[str, object]) -> dict[str, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    summary: dict[str, int] = {}
    for key, value in usage.items():
        if type(value) is int:
            summary[str(key)] = value
    return summary or None


def endpoint_summary(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return f"{parts.scheme}://{netloc}{parts.path}"
