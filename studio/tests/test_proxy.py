"""Tests for proxy.py — egress proxy with manifest enforcement, DNS pinning, SNI sniffing."""

import asyncio
import json
import os
import struct
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from studio.orchestrator.proxy import (
    _resolve_host,
    _extract_sni,
    _host_matches,
    DnsCache,
    DnsError,
    EgressProxy,
)
from studio.orchestrator.models import (
    CapabilityManifest,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    FilesystemGrants,
    EgressGrant,
    EgressProxySettings,
    NetworkGrants,
    IngressConfig,
    DnsConfig,
    ProcessGrants,
    RpcGrants,
    ResourceGrants,
    Grants,
    ManifestSubject,
    ManifestMetadata,
    WorkingTree,
)


def make_manifest(*, egress: list[dict] | None = None, dns_enabled: bool = True) -> CapabilityManifest:
    """Build a CapabilityManifest with specified network egress grants."""
    egress_grants = []
    for e in (egress or []):
        egress_grants.append(EgressGrant(
            destination=e.get("destination", "api.github.com"),
            ports=e.get("ports", [443]),
            protocol=e.get("protocol", "https"),
            rationale=e.get("rationale", "test"),
        ))
    return CapabilityManifest(
        schema_version="1.0",
        subject=ManifestSubject(kind="bundle", id="test"),
        grants=Grants(
            filesystem=FilesystemGrants(
                reads=[FilesystemPathGrant(path="/usr/lib", recursive=True)],
                writes=[],
                working_tree=WorkingTree(branch="test", base="main", write_scope="full"),
            ),
            network=NetworkGrants(
                egress=egress_grants,
                ingress=IngressConfig(enabled=False),
                dns=DnsConfig(enabled=dns_enabled),
            ),
            process=ProcessGrants(),
            rpc=RpcGrants(methods=["worker.*"]),
            resources=ResourceGrants(),
        ),
        metadata=ManifestMetadata(rationale="test"),
    )


# ── _host_matches tests ────────────────────────────────────────────────────────


class TestHostMatching:
    def test_exact_hostname_match(self):
        assert _host_matches("api.github.com", "api.github.com")

    def test_exact_hostname_mismatch(self):
        assert not _host_matches("evil.com", "api.github.com")

    def test_subdomain_not_matched(self):
        assert not _host_matches("foo.api.github.com", "api.github.com")

    def test_cidr_match(self):
        assert _host_matches("10.0.0.5", "10.0.0.0/24") is True

    def test_cidr_no_match(self):
        assert _host_matches("192.168.1.5", "10.0.0.0/24") is False

    def test_cidr_ip_not_in_cidr_range(self):
        assert _host_matches("not-an-ip", "10.0.0.0/24") is False


# ── _resolve_host tests ────────────────────────────────────────────────────────


class TestResolveHost:
    def test_allowed_host(self):
        manifest = make_manifest(egress=[
            {"destination": "api.github.com", "ports": [443]},
        ])
        grant = _resolve_host("api.github.com", 443, manifest)
        assert grant is not None
        assert grant.destination == "api.github.com"

    def test_denied_host(self):
        manifest = make_manifest(egress=[
            {"destination": "api.github.com", "ports": [443]},
        ])
        grant = _resolve_host("evil.com", 443, manifest)
        assert grant is None

    def test_wrong_port(self):
        manifest = make_manifest(egress=[
            {"destination": "api.github.com", "ports": [443]},
        ])
        grant = _resolve_host("api.github.com", 8080, manifest)
        assert grant is None

    def test_empty_ports_means_any_port(self):
        manifest = make_manifest(egress=[
            {"destination": "api.github.com", "ports": []},
        ])
        grant = _resolve_host("api.github.com", 9999, manifest)
        assert grant is not None

    def test_no_egress_grants(self):
        manifest = make_manifest(egress=[])
        grant = _resolve_host("api.github.com", 443, manifest)
        assert grant is None

    def test_multiple_grants_first_matches(self):
        manifest = make_manifest(egress=[
            {"destination": "api.github.com", "ports": [443]},
            {"destination": "pypi.org", "ports": [443]},
        ])
        grant = _resolve_host("pypi.org", 443, manifest)
        assert grant is not None
        assert grant.destination == "pypi.org"


# ── _extract_sni tests ─────────────────────────────────────────────────────────


class TestSniExtraction:
    def make_client_hello(self, sni: str) -> bytes:
        """Build a valid TLS ClientHello with SNI extension."""
        sni_bytes = sni.encode("ascii")
        sni_ext_data = (
            b"\x00" +  # name_type = hostname
            struct.pack(">H", len(sni_bytes)) +  # name length
            sni_bytes
        )
        sni_ext = (
            struct.pack(">H", 0x0000) +  # extension_type = SNI
            struct.pack(">H", 2 + len(sni_ext_data)) +  # extension length
            struct.pack(">H", len(sni_ext_data)) +  # server_name_list length
            sni_ext_data
        )
        extensions_len = len(sni_ext)
        extensions = struct.pack(">H", extensions_len) + sni_ext

        # Build ClientHello handshake message
        client_hello = (
            b"\x01" +  # handshake type = ClientHello
            b"\x00\x00\x00" +  # length placeholder (3 bytes)
            b"\x03\x03" +  # protocol version TLS 1.2
            b"\x00" * 32 +  # random
            b"\x00" +  # session_id length = 0
            b"\x00\x02\x00\x3c" +  # cipher suites (1 suite = 0x003c)
            b"\x01\x00"  # compression methods (1 = null)
        )
        # Fix handshake length
        hs_len = len(client_hello) - 4 + len(extensions)
        client_hello = (
            b"\x01" +
            struct.pack(">I", hs_len)[1:] +  # 3-byte big-endian length
            client_hello[4:]
        )
        # Add extensions
        client_hello += extensions

        # Wrap in TLS record
        content_type = b"\x16"  # handshake
        tls_version = b"\x03\x01"  # TLS 1.0
        tls_record = content_type + tls_version + struct.pack(">H", len(client_hello)) + client_hello

        return tls_record

    def test_extract_sni(self):
        data = self.make_client_hello("api.github.com")
        sni = _extract_sni(data)
        assert sni == "api.github.com"

    def test_extract_sni_other_host(self):
        data = self.make_client_hello("pypi.org")
        sni = _extract_sni(data)
        assert sni == "pypi.org"

    def test_not_a_client_hello(self):
        data = b"\x17\x03\x03\x00\x10" + b"\x00" * 16  # application data
        sni = _extract_sni(data)
        assert sni is None

    def test_too_short(self):
        sni = _extract_sni(b"\x16\x03\x01")
        assert sni is None

    def test_empty_sni(self):
        data = self.make_client_hello("")
        sni = _extract_sni(data)
        assert sni == ""

    def test_client_hello_no_sni_extension(self):
        """Build a minimal ClientHello with no SNI extension."""
        client_hello = (
            b"\x01\x00\x00\x29" +  # handshake type + length
            b"\x03\x03" +  # TLS 1.2
            b"\x00" * 32 +  # random
            b"\x00" +  # session_id length 0
            b"\x00\x02\x00\x3c" +  # cipher suites
            b"\x01\x00" +  # compression methods
            b"\x00\x00"  # extensions length = 0
        )
        tls_record = b"\x16\x03\x01" + struct.pack(">H", len(client_hello)) + client_hello
        sni = _extract_sni(tls_record)
        assert sni is None


# ── DnsCache tests ─────────────────────────────────────────────────────────────


class TestDnsCache:
    def test_resolve_and_pin_returns_pinned_ip(self):
        manifest = make_manifest()
        cache = DnsCache(manifest)

        async def run():
            cache._pinned["example.com"] = "93.184.216.34"
            ip = await cache.resolve_and_pin("example.com")
            return ip

        ip = asyncio.run(run())
        assert ip == "93.184.216.34"

    def test_resolve_and_pin_caches_ip(self):
        manifest = make_manifest()
        cache = DnsCache(manifest)

        mock_addrinfo = [
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]

        async def run():
            loop = asyncio.get_running_loop()
            loop.getaddrinfo = AsyncMock(return_value=mock_addrinfo)
            ip = await cache.resolve_and_pin("example.com")
            assert ip == "93.184.216.34"
            assert "example.com" in cache._pinned
            assert cache._pinned["example.com"] == "93.184.216.34"

        asyncio.run(run())

    def test_dns_disabled_by_manifest(self):
        manifest = make_manifest(dns_enabled=False)
        cache = DnsCache(manifest)

        async def run():
            with pytest.raises(DnsError, match="DNS is disabled"):
                await cache.resolve_and_pin("example.com")

        asyncio.run(run())

    def test_dns_resolution_failure(self):
        manifest = make_manifest()
        cache = DnsCache(manifest)

        async def run():
            loop = asyncio.get_running_loop()
            import socket
            loop.getaddrinfo = AsyncMock(side_effect=socket.gaierror("Name or service not known"))
            with pytest.raises(DnsError, match="DNS resolution failed"):
                await cache.resolve_and_pin("nonexistent.example")

        asyncio.run(run())


# ── EgressProxy tests ──────────────────────────────────────────────────────────


class TestEgressProxy:
    @pytest.fixture
    def manifest(self):
        return make_manifest(egress=[
            {"destination": "api.github.com", "ports": [443]},
            {"destination": "pypi.org", "ports": [443, 80]},
        ])

    def test_construction(self, manifest):
        proxy = EgressProxy("/run/studio/proxy-w1.sock", manifest)
        assert proxy._socket_path == "/run/studio/proxy-w1.sock"
        assert proxy._manifest is manifest

    def test_start_and_shutdown(self, manifest):
        async def run():
            socket_path = f"/tmp/test-proxy-{os.getpid()}.sock"
            proxy = EgressProxy(socket_path, manifest)

            server_task = asyncio.create_task(proxy.run())
            await asyncio.sleep(0.05)

            await proxy.shutdown()
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

            assert not os.path.exists(socket_path)

        asyncio.run(run())

    def test_http_forwarding_denied_host(self, manifest):
        """HTTP requests to unauthorized hosts must be denied."""
        grant = _resolve_host("evil.com", 80, manifest)
        assert grant is None

    def test_http_forwarding_allowed_host_deny_port(self, manifest):
        """Port 22 on an allowed host should be denied."""
        grant = _resolve_host("api.github.com", 22, manifest)
        assert grant is None

    def test_connect_allowed(self, manifest):
        """CONNECT to an allowed host:port should pass the manifest check."""
        grant = _resolve_host("api.github.com", 443, manifest)
        assert grant is not None

    def test_connect_denied(self, manifest):
        """CONNECT to a denied host should fail."""
        grant = _resolve_host("evil.com", 443, manifest)
        assert grant is None

    def test_http_proxy_url_parsing(self, manifest):
        """HTTP proxy validates the Host header from the request URL."""
        grant = _resolve_host("pypi.org", 80, manifest)
        assert grant is not None
        assert grant.destination == "pypi.org"

    def test_http_without_host_header(self, manifest):
        """HTTP request without Host header validates against URL hostname."""
        # api.github.com:443 is allowed
        grant = _resolve_host("api.github.com", 443, manifest)
        assert grant is not None
        # Only port 443 is allowed for api.github.com
        grant_port_80 = _resolve_host("api.github.com", 80, manifest)
        assert grant_port_80 is None


class TestEgressProxySettings:
    def test_defaults(self):
        settings = EgressProxySettings()
        assert settings.enabled is True
        assert settings.socket_dir == "/run/studio"
        assert settings.connect_timeout_seconds == 10
        assert settings.read_timeout_seconds == 30

    def test_disabled(self):
        settings = EgressProxySettings(enabled=False)
        assert settings.enabled is False

    def test_custom_socket_dir(self):
        settings = EgressProxySettings(socket_dir="/tmp/custom")
        assert settings.socket_dir == "/tmp/custom"
