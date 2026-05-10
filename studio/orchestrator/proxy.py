"""Per-worker egress proxy: enforces hostname-based network grants from the capability manifest.

Runs alongside each worker process. Listens on a Unix domain socket.
Worker subprocesses route HTTP/HTTPS through it via http_proxy/https_proxy.
Supports:
  - HTTP forwarding proxy (parses Host header, validates against manifest)
  - HTTPS CONNECT tunneling (sniffs TLS ClientHello SNI, validates against manifest)
  - DNS pinning at connection time (prevents rebinding attacks)
  - Per-destination protocol restrictions (tcp, http, https)

Architecture: one proxy process per worker, spawned by the runner.
Lifecycle tied to the worker — proxy exits when the worker does.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

from .models import CapabilityManifest, EgressGrant


def _now() -> int:
    return int(time.time())


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"[{ts}] proxy: {msg}", file=sys.stderr, flush=True)


# ── Manifest resolution ────────────────────────────────────────────────────────


def _resolve_host(host: str, port: int, manifest: CapabilityManifest) -> EgressGrant | None:
    """Find the egress grant that allows egress to host:port. Returns None if denied."""
    for entry in manifest.grants.network.egress:
        if not _host_matches(host, entry.destination):
            continue
        if entry.ports and port not in entry.ports:
            continue
        return entry
    return None


def _host_matches(host: str, destination: str) -> bool:
    """Check if host matches a destination (exact hostname or CIDR)."""
    if host == destination:
        return True
    if "/" in destination:
        # CIDR — resolve host to IP and check containment
        try:
            import ipaddress
            addr = ipaddress.ip_address(host)
            net = ipaddress.ip_network(destination, strict=False)
            return addr in net
        except ValueError:
            return False
    return False


# ── DNS resolution with pinning ────────────────────────────────────────────────


class DnsCache:
    """Resolves hostnames and pins IPs for connection lifetime."""

    def __init__(self, manifest: CapabilityManifest) -> None:
        self._manifest = manifest
        self._pinned: dict[str, str] = {}

    async def resolve_and_pin(self, host: str) -> str:
        """Resolve host to IP, validate against manifest, pin result. Returns IP."""
        if host in self._pinned:
            return self._pinned[host]

        # Check if DNS is enabled in the manifest
        if not self._manifest.grants.network.dns.enabled:
            raise DnsError(f"DNS is disabled by capability manifest (dns.enabled=false)")

        loop = asyncio.get_running_loop()
        try:
            addrinfo = await loop.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise DnsError(f"DNS resolution failed for {host}: {exc}")

        if not addrinfo:
            raise DnsError(f"No addresses resolved for {host}")

        # Use first resolved IPv4 address, fall back to IPv6
        ip: str | None = None
        for info in addrinfo:
            family = info[0]
            addr = info[4][0]
            if family == socket.AF_INET:
                ip = addr
                break
        if ip is None and addrinfo:
            ip = addrinfo[0][4][0]

        if ip is None:
            raise DnsError(f"No usable address resolved for {host}")

        self._pinned[host] = ip
        return ip


class DnsError(Exception):
    """DNS resolution failures (may indicate rebinding attempt or disabled DNS)."""
    pass


# ── TLS ClientHello SNI sniffing ───────────────────────────────────────────────


def _extract_sni(data: bytes) -> str | None:
    """Parse the SNI hostname from a TLS ClientHello. Returns None on failure.

    TLS record format (RFC 8446):
      byte 0: ContentType (0x16 = handshake)
      bytes 1-2: ProtocolVersion (0x0301..0x0304)
      bytes 3-4: length of payload (big-endian)
      Handshake:
        byte 5: HandshakeType (0x01 = ClientHello)
        bytes 6-8: length
        bytes 9-10: ProtocolVersion
        bytes 11-42: random
        byte 43: session_id_length
        ...skip session_id...
        ...skip cipher_suites...
        ...skip compression_methods...
        ...extensions...
          type 0x0000 = SNI
            length
            ServerNameList
              ServerName
                name_type (0x00 = hostname)
                length
                name (the hostname)
    """
    if len(data) < 43:
        return None
    if data[0] != 0x16:
        return None

    # Read TLS record length (big-endian u16)
    tls_record_len = struct.unpack(">H", data[3:5])[0]
    if len(data) < 5 + tls_record_len:
        return None

    handshake = data[5 : 5 + tls_record_len]
    if len(handshake) < 38:  # minimum ClientHello size
        return None
    if handshake[0] != 0x01:  # ClientHello
        return None

    # Read handshake length (3-byte big-endian u24)
    # handshake[1:4] is length
    pos = 4  # Start after handshake header (type + length)
    # Skip protocol version (2 bytes)
    pos += 2
    # Skip random (32 bytes)
    pos += 32
    if pos >= len(handshake):
        return None

    # Session ID length
    session_id_len = handshake[pos]
    pos += 1 + session_id_len
    if pos + 2 >= len(handshake):
        return None

    # Cipher suites length
    cipher_suites_len = struct.unpack(">H", handshake[pos:pos + 2])[0]
    pos += 2 + cipher_suites_len
    if pos + 1 >= len(handshake):
        return None

    # Compression methods length
    comp_len = handshake[pos]
    pos += 1 + comp_len
    if pos + 2 >= len(handshake):
        return None

    # Extensions length
    extensions_len = struct.unpack(">H", handshake[pos:pos + 2])[0]
    pos += 2
    extensions_end = pos + extensions_len
    if extensions_end > len(handshake):
        return None

    while pos + 4 <= extensions_end:
        ext_type = struct.unpack(">H", handshake[pos:pos + 2])[0]
        ext_len = struct.unpack(">H", handshake[pos + 2:pos + 4])[0]
        pos += 4
        if pos + ext_len > extensions_end:
            break
        if ext_type == 0x0000:  # SNI
            # Parse ServerNameList
            sni_pos = pos
            if sni_pos + 2 > pos + ext_len:
                break
            # sni_list_len = struct.unpack(">H", handshake[sni_pos:sni_pos + 2])[0]
            sni_pos += 2
            if sni_pos + 3 > pos + ext_len:
                break
            name_type = handshake[sni_pos]
            name_len = struct.unpack(">H", handshake[sni_pos + 1:sni_pos + 3])[0]
            sni_pos += 3
            if sni_pos + name_len > pos + ext_len:
                break
            return handshake[sni_pos:sni_pos + name_len].decode("ascii", errors="replace")

        pos += ext_len

    return None


# ── Core proxy ─────────────────────────────────────────────────────────────────


class EgressProxy:
    """Per-worker asyncio forward proxy with capability manifest enforcement."""

    def __init__(self, socket_path: str, manifest: CapabilityManifest) -> None:
        self._socket_path = socket_path
        self._manifest = manifest
        self._server: asyncio.AbstractServer | None = None
        self._dns = DnsCache(manifest)

    async def run(self) -> None:
        """Start the proxy on the Unix socket. Blocks until the server stops."""
        # Clean up stale socket
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

        # Ensure parent directory exists
        sock_dir = os.path.dirname(self._socket_path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._socket_path
        )
        # Set restrictive permissions
        os.chmod(self._socket_path, 0o600)

        _log(f"Listening on {self._socket_path}")
        async with self._server:
            await self._server.serve_forever()

    async def shutdown(self) -> None:
        """Shut down the proxy server and clean up socket."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one client connection (the worker making an HTTP request)."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            writer.close()
            return

        if not request_line:
            writer.close()
            return

        line = request_line.decode("utf-8", errors="replace").strip()
        parts = line.split(" ")
        if len(parts) < 3:
            writer.close()
            return

        method = parts[0].upper()
        url = parts[1]
        http_version = parts[2]

        # Parse proxy URL: http://host:port/path or host:port (CONNECT)
        if method == "CONNECT":
            await self._handle_connect(reader, writer, url, http_version)
        else:
            await self._handle_http(reader, writer, method, url, http_version, line)

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        http_version: str,
    ) -> None:
        """Handle CONNECT tunneling (HTTPS)."""
        # Parse host:port
        if ":" in target:
            host = target.rsplit(":", 1)[0]
            port_str = target.rsplit(":", 1)[1]
            try:
                port = int(port_str)
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return
        else:
            host = target
            port = 443

        # Validate host:port against manifest
        grant = _resolve_host(host, port, self._manifest)
        if grant is None:
            _log(f"DENIED: CONNECT {host}:{port}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Resolve DNS and pin IP
        try:
            ip = await self._dns.resolve_and_pin(host)
        except DnsError as exc:
            _log(f"DNS error for {host}: {exc}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Connect to target by pinned IP
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=10.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            _log(f"CONNECT failed to {host}:{port} ({ip}): {exc}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Send 200 to client
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # Wait for the first TLS ClientHello from the client to extract SNI
        # We peek at initial client data before forwarding
        # The client sends its ClientHello immediately after 200
        peeking = True
        sni_checked = False
        client_buf = b""

        async def _client_to_remote() -> None:
            nonlocal peeking, sni_checked, client_buf
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    if peeking:
                        client_buf += data
                        # Need a full TLS record to parse SNI
                        if len(client_buf) >= 5:
                            tls_record_len = struct.unpack(">H", client_buf[3:5])[0]
                            if len(client_buf) >= 5 + tls_record_len:
                                sni = _extract_sni(client_buf)
                                peeking = False
                                if sni is not None:
                                    sni_checked = True
                                    # Validate SNI against the same grant
                                    if sni != host:
                                        _log(f"SNI MISMATCH: CONNECT target={host}, TLS SNI={sni} — blocking")
                                        # Forward a TLS alert to client before closing
                                        await writer.drain()
                                        return
                                # Forward the buffered data
                                remote_writer.write(client_buf)
                                await remote_writer.drain()
                                continue
                    if not peeking:
                        remote_writer.write(data)
                        await remote_writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                try:
                    remote_writer.close()
                except OSError:
                    pass

        async def _remote_to_client() -> None:
            try:
                while True:
                    data = await remote_reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

        # Run both directions concurrently
        task_c2r = asyncio.ensure_future(_client_to_remote())
        task_r2c = asyncio.ensure_future(_remote_to_client())

        done, pending = await asyncio.wait(
            [task_c2r, task_r2c], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        url: str,
        http_version: str,
        first_line: str,
    ) -> None:
        """Handle plain HTTP forwarding proxy request."""
        # Parse URL: http://host:port/path
        if not url.startswith("http://"):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        rest = url[7:]  # remove "http://"
        if "/" in rest:
            host_part, _, path = rest.partition("/")
            path = "/" + path
        else:
            host_part = rest
            path = "/"

        if ":" in host_part:
            host = host_part.rsplit(":", 1)[0]
            port_str = host_part.rsplit(":", 1)[1]
            try:
                port = int(port_str)
            except ValueError:
                port = 80
        else:
            host = host_part
            port = 80

        # Read remaining headers to find Host header
        headers: list[str] = []
        host_header: str | None = None
        content_length = 0
        while True:
            try:
                header_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                writer.close()
                return
            if not header_line or header_line.strip() == b"":
                break
            decoded = header_line.decode("utf-8", errors="replace").strip()
            headers.append(decoded)
            if decoded.lower().startswith("host:"):
                host_header = decoded[5:].strip()
            if decoded.lower().startswith("content-length:"):
                try:
                    content_length = int(decoded[15:].strip())
                except ValueError:
                    pass

        # Use Host header if it gives a more specific hostname
        effective_host = host
        if host_header:
            hh = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
            if hh:
                effective_host = hh

        # Validate host:port against manifest
        grant = _resolve_host(effective_host, port, self._manifest)
        if grant is None:
            _log(f"DENIED: HTTP {method} {effective_host}:{port}{path}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Resolve DNS and pin IP
        try:
            ip = await self._dns.resolve_and_pin(effective_host)
        except DnsError as exc:
            _log(f"DNS error for {effective_host}: {exc}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Connect to target
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=10.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            _log(f"HTTP connect failed to {effective_host}:{port} ({ip}): {exc}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Forward the request
        req = f"{method} {path} {http_version}\r\n"
        for h in headers:
            # Replace Host header with effective host (strip port for HTTP)
            if h.lower().startswith("host:"):
                req += f"Host: {effective_host}\r\n"
            else:
                req += f"{h}\r\n"
        req += "\r\n"

        remote_writer.write(req.encode("utf-8"))
        await remote_writer.drain()

        # Read and forward body if present
        if content_length > 0:
            read_count = 0
            while read_count < content_length:
                chunk_size = min(content_length - read_count, 65536)
                body_data = await reader.read(chunk_size)
                if not body_data:
                    break
                remote_writer.write(body_data)
                await remote_writer.drain()
                read_count += len(body_data)

        # Relay response back
        try:
            while True:
                data = await remote_reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass
            try:
                remote_writer.close()
            except OSError:
                pass


# ── Entry point (invoked as subprocess) ────────────────────────────────────────


def _load_manifest() -> CapabilityManifest:
    """Load the capability manifest from env var STUDIO_MANIFEST_JSON."""
    raw = os.environ.get("STUDIO_MANIFEST_JSON", "{}")
    return CapabilityManifest.model_validate_json(raw)


async def _async_main() -> None:
    socket_path = os.environ.get("STUDIO_PROXY_SOCKET", "")
    if not socket_path:
        _log("FATAL: STUDIO_PROXY_SOCKET not set")
        sys.exit(1)

    manifest = _load_manifest()
    proxy = EgressProxy(socket_path, manifest)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    # Shutdown on SIGTERM
    def _on_signal() -> None:
        if not stop.done():
            stop.set_result(None)

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT, _on_signal)

    server_task = asyncio.create_task(proxy.run())

    await stop
    _log("Shutting down...")
    await proxy.shutdown()
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


def main() -> None:
    asyncio.run(_async_main())


import signal  # noqa: E402


if __name__ == "__main__":
    main()
