"""
Minimal client for Amethyst's headless JSON-RPC service (``headless --stdio``).

The Qt panel starts one backend from ``context["headless_command"]`` and uses
it from a worker thread only.  Job-producing methods (``*.start``) are polled
with ``jobs.get`` until they finish.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Optional


class RpcError(RuntimeError):
    def __init__(self, message: str, code: Any = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class Headless:
    def __init__(self, command: list[str], client_name: str, client_version: str):
        self._cmd = list(command)
        self._name = client_name
        self._version = client_version
        self._proc: Optional[subprocess.Popen] = None
        self._next = 1
        self._lock = threading.Lock()
        self.handshake: dict = {}

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.handshake = self.call("system.handshake", {
            "protocol_version": 1, "client": {"name": self._name, "version": self._version}})

    def close(self, timeout: float = 120) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=timeout)  # the service finishes accepted jobs first
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        with self._lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                raise RpcError("Amethyst backend is not running")
            rid = self._next
            self._next += 1
            self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                               "params": params or {}}) + "\n")
            self._proc.stdin.flush()
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise RpcError("Amethyst backend exited unexpectedly")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") != rid:
                    continue
                if "error" in msg and msg["error"]:
                    err = msg["error"]
                    raise RpcError(err.get("message", str(err)), err.get("code"), err.get("data"))
                return msg.get("result")

    def run_job(self, method: str, params: dict, timeout: float = 600) -> Any:
        """Start a job and wait for its result; raise RpcError if it fails."""
        snap = self.call(method, params)
        job = (snap or {}).get("job") or {}
        jid = job.get("id")
        deadline = time.monotonic() + timeout
        while job.get("state") not in ("succeeded", "failed", "cancelled", "interrupted"):
            if time.monotonic() > deadline:
                raise RpcError(f"{method} did not finish in time")
            time.sleep(0.2)
            job = (self.call("jobs.get", {"job_id": jid}) or {}).get("job") or {}
        if job.get("state") != "succeeded":
            err = job.get("error") or {}
            raise RpcError(err.get("message") or f"{method} {job.get('state')}",
                           err.get("code"), err.get("details"))
        return job.get("result")
