#!/usr/bin/env python3
"""Small OpenAI-compatible bridge for OpenCode's official CLI.

The OpenCode free tier rejects direct third-party API requests. This bridge keeps
the request inside the official ``opencode run`` process and exposes only the two
explicitly approved free models on loopback. A custom OpenCode tool translates
model tool requests back into OpenAI-compatible tool calls for ARTEX to execute.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time


PORT = int(os.environ.get("OPENCODE_BRIDGE_PORT", "14059"))
TOKEN = os.environ["OPENCODE_BRIDGE_TOKEN"]
TIMEOUT = int(os.environ.get("OPENCODE_TIMEOUT", "300"))
OPENCODE_BIN = os.environ.get(
    "OPENCODE_BIN", "/opt/artex/opencode-free/node_modules/.bin/opencode"
)
WORKSPACE = os.environ.get("OPENCODE_WORKSPACE", "/opt/artex/opencode-free/workspace")
MAX_BODY = 16 * 1024 * 1024
RUN_SLOTS = threading.BoundedSemaphore(int(os.environ.get("OPENCODE_BRIDGE_CONCURRENCY", "2")))

MODELS = (
    "mimo-v2.6-flash-free",
    "muse-spark-1.3-contributor-free",
)


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def build_prompt(messages: list[dict], tools: list[dict]) -> str:
    chunks: list[str] = []
    if tools:
        catalog = []
        for entry in tools:
            function = entry.get("function") or {}
            name = str(function.get("name", "")).strip()
            if not name:
                continue
            catalog.append(
                {
                    "name": name,
                    "description": str(function.get("description", "")),
                    "parameters": function.get("parameters") or {"type": "object"},
                }
            )
        chunks.append(
            "System: ARTEX owns the external tools listed below. When a tool is needed, "
            "do not use OpenCode built-in tools. Call the OpenCode tool artex_proxy with "
            "name set to the exact ARTEX tool name and arguments set to a JSON object "
            "encoded as a string. After artex_proxy returns, stop immediately; ARTEX will "
            "execute the call and provide its result in the next request. If no tool is "
            "needed, answer normally. Available ARTEX tools:\n"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )
    for message in messages:
        role = str(message.get("role", "user"))
        text = content_text(message.get("content", "")).strip()
        if text:
            label = {
                "system": "System",
                "assistant": "Assistant",
                "user": "User",
                "tool": "ARTEX tool result",
            }.get(role, role.title())
            call_id = str(message.get("tool_call_id", "")).strip()
            suffix = f" ({call_id})" if role == "tool" and call_id else ""
            chunks.append(f"{label}{suffix}: {text}")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            chunks.append(
                "Assistant requested ARTEX tool "
                + str(function.get("name", ""))
                + " with arguments "
                + str(function.get("arguments", "{}"))
                + "."
            )
    return "\n\n".join(chunks)


def complete(model: str, messages: list[dict], tools: list[dict]) -> tuple[str, list[dict]]:
    command = [
        OPENCODE_BIN,
        "run",
        "--format",
        "json",
        "--model",
        f"opencode/{model}",
    ]
    session_id = ""
    try:
        process = subprocess.run(
            command,
            input=build_prompt(messages, tools),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKSPACE,
            env=os.environ.copy(),
            timeout=TIMEOUT,
            check=False,
        )
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        errors: list[str] = []
        allowed = {
            str((entry.get("function") or {}).get("name", ""))
            for entry in tools
            if (entry.get("function") or {}).get("name")
        }
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = session_id or str(event.get("sessionID", ""))
            if event.get("type") == "text":
                part = event.get("part") or {}
                text_parts.append(str(part.get("text", "")))
            elif event.get("type") == "tool_use":
                part = event.get("part") or {}
                state = part.get("state") or {}
                request = state.get("input") or {}
                if part.get("tool") != "artex_proxy" or state.get("status") != "completed":
                    continue
                name = str(request.get("name", ""))
                arguments = request.get("arguments", "{}")
                if name not in allowed:
                    errors.append(f"OpenCode requested an unknown ARTEX tool: {name}")
                    continue
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                try:
                    parsed = json.loads(arguments)
                    if not isinstance(parsed, dict):
                        raise ValueError("tool arguments must be an object")
                    arguments = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                except (json.JSONDecodeError, ValueError):
                    errors.append(f"OpenCode returned invalid JSON arguments for tool {name}")
                    continue
                tool_calls.append(
                    {
                        "id": str(part.get("callID") or f"call_{secrets.token_hex(12)}"),
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
            elif event.get("type") == "error":
                data = ((event.get("error") or {}).get("data") or {})
                errors.append(str(data.get("message") or event.get("error") or "OpenCode error"))
        text = "".join(text_parts).strip()
        if tool_calls:
            return "", tool_calls
        if not text:
            detail = errors[-1] if errors else process.stderr.strip() or f"exit status {process.returncode}"
            raise RuntimeError(f"OpenCode returned no visible text: {detail[:1000]}")
        return text, []
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"OpenCode timed out after {TIMEOUT}s") from exc
    finally:
        if session_id:
            try:
                cleanup = subprocess.run(
                    [OPENCODE_BIN, "session", "delete", session_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=WORKSPACE,
                    env=os.environ.copy(),
                    timeout=30,
                    check=False,
                )
            except Exception:
                cleanup = None
            if cleanup is None or cleanup.returncode != 0:
                print("[bridge] session cleanup failed", file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "artex-opencode-free-bridge/1"

    def authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {TOKEN}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self.authorized():
            self.send_json({"error": {"message": "unauthorized"}}, 401)
            return
        if self.path == "/v1/models":
            self.send_json({"object": "list", "data": [{"id": model, "object": "model"} for model in MODELS]})
            return
        if self.path in ("/health", "/v1/health"):
            try:
                check = subprocess.run(
                    [OPENCODE_BIN, "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=15,
                    check=False,
                )
                ok = check.returncode == 0
                version = check.stdout.strip()
            except Exception:
                ok, version = False, ""
            self.send_json({"ok": ok, "upstream_version": version, "models": list(MODELS)}, 200 if ok else 503)
            return
        self.send_json({"error": {"message": "not found"}}, 404)

    def do_POST(self) -> None:
        if not self.authorized():
            self.send_json({"error": {"message": "unauthorized"}}, 401)
            return
        if self.path != "/v1/chat/completions":
            self.send_json({"error": {"message": "not found"}}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": {"message": "invalid content length"}}, 400)
            return
        if length <= 0 or length > MAX_BODY:
            self.send_json({"error": {"message": "request body size is invalid"}}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model = str(body.get("model", ""))
        if model not in MODELS:
            self.send_json({"error": {"message": f"model is not allowed: {model}"}}, 400)
            return
        if not RUN_SLOTS.acquire(timeout=10):
            self.send_json(
                {"error": {"message": "OpenCode Free bridge is busy", "type": "rate_limit"}},
                429,
            )
            return
        try:
            text, tool_calls = complete(model, body.get("messages") or [], body.get("tools") or [])
        except Exception as exc:
            self.send_json({"error": {"message": str(exc)[:1000], "type": "upstream_error"}}, 502)
            return
        finally:
            RUN_SLOTS.release()
        completion_id = f"chatcmpl-{secrets.token_hex(12)}"
        created = int(time.time())
        finish_reason = "tool_calls" if tool_calls else "stop"
        if body.get("stream"):
            delta: dict = {"role": "assistant"}
            if tool_calls:
                delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(tool_calls)]
            else:
                delta["content"] = text
            frames = [
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                },
            ]
            data = ("".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_json(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": text,
                            **({"tool_calls": tool_calls} if tool_calls else {}),
                        },
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[bridge] {self.client_address[0]} {fmt % args}", file=sys.stderr, flush=True)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


if __name__ == "__main__":
    server = Server(("127.0.0.1", PORT), Handler)
    print(f"OpenCode Free bridge listening on 127.0.0.1:{PORT} via opencode run", flush=True)
    server.serve_forever()
