"""Shared JSON-RPC 2.0 client supporting Unix socket and TCP/mTLS transports.

Used by all worker types (developer, bundler, review, qa).

TCP connections require mutual TLS. Cert paths are read from environment:
  STUDIO_WORKER_CERT - path to worker certificate PEM file
  STUDIO_WORKER_KEY  - path to worker private key PEM file
  STUDIO_ORCHESTRATOR_CA - path to CA certificate PEM file
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path
from typing import Any


def _parse_orchestrator_addr() -> tuple[str, str | None, str | int | None]:
    """Parse STUDIO_ORCHESTRATOR_ADDR or fall back to STUDIO_SOCKET_PATH.

    Returns (scheme, host_or_path, port_or_None).
    scheme is "unix" or "tcp".
    """
    addr = os.environ.get("STUDIO_ORCHESTRATOR_ADDR", "")
    socket_path = os.environ.get("STUDIO_SOCKET_PATH", "/run/studio/orchestrator.sock")

    if addr:
        if addr.startswith("tcp://"):
            host_port = addr[6:]
            if ":" in host_port:
                host, port_str = host_port.rsplit(":", 1)
                return ("tcp", host, int(port_str))
            return ("tcp", host_port, 7811)
        elif addr.startswith("unix:"):
            return ("unix", addr[5:], None)
        else:
            return ("unix", addr, None)
    return ("unix", socket_path, None)


def get_orchestrator_addr_display() -> str:
    """Human-readable address for log messages."""
    return os.environ.get("STUDIO_ORCHESTRATOR_ADDR") or os.environ.get(
        "STUDIO_SOCKET_PATH", "/run/studio/orchestrator.sock"
    )


def _build_mtls_context() -> ssl.SSLContext:
    """Build an mTLS client SSL context from env-provided cert/key/CA paths."""
    cert_path = os.environ.get("STUDIO_WORKER_CERT", "")
    key_path = os.environ.get("STUDIO_WORKER_KEY", "")
    ca_path = os.environ.get("STUDIO_ORCHESTRATOR_CA", "")

    if not cert_path or not key_path or not ca_path:
        raise RuntimeError(
            "TCP transport requires mTLS: set STUDIO_WORKER_CERT, STUDIO_WORKER_KEY, "
            "and STUDIO_ORCHESTRATOR_CA environment variables"
        )

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_path)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


class RpcClient:
    """Minimal JSON-RPC 2.0 client over Unix socket or TCP/mTLS."""

    def __init__(self) -> None:
        scheme, target, port = _parse_orchestrator_addr()
        self._scheme = scheme
        self._target = target
        self._port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._req_id = 0

    async def connect(self) -> None:
        if self._scheme == "unix":
            self.reader, self.writer = await asyncio.open_unix_connection(self._target)
        else:
            tls_ctx = _build_mtls_context()
            self.reader, self.writer = await asyncio.open_connection(
                self._target, self._port, ssl=tls_ctx,
            )

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._req_id += 1
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._req_id,
        }
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

        line = await self.reader.readline()
        if not line:
            return {"error": {"code": -1, "message": "Connection closed"}}

        return json.loads(line.decode("utf-8"))

    async def notify(self, method: str, params: dict | None = None) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

    async def receive(self, timeout: float = 0.1) -> dict | None:
        """Non-blocking read of an incoming message from the orchestrator.

        Returns the parsed JSON message dict, or None if no message is available
        within the timeout or if the connection is closed.
        """
        if self.reader is None:
            return None
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        except (asyncio.TimeoutError, Exception):
            return None
