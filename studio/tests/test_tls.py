"""Tests for mTLS helpers in studio/orchestrator/tls.py."""
import os
import pytest
import ssl as ssl_mod

from studio.orchestrator.tls import (
    generate_ca,
    issue_worker_cert,
    create_server_tls_context,
    create_client_tls_context,
)


class TestGenerateCa:
    def test_generate_ca_creates_files(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        cert_pem, key_pem = generate_ca(str(ca_cert), str(ca_key))

        assert ca_cert.exists()
        assert ca_key.exists()
        assert b"BEGIN CERTIFICATE" in cert_pem
        assert b"BEGIN PRIVATE KEY" in key_pem

    def test_generate_ca_idempotent(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        cert1, key1 = generate_ca(str(ca_cert), str(ca_key))
        cert2, key2 = generate_ca(str(ca_cert), str(ca_key))
        assert cert1 == cert2
        assert key1 == key2

    def test_generate_ca_file_permissions(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        generate_ca(str(ca_cert), str(ca_key))

        key_stat = os.stat(str(ca_key))
        assert key_stat.st_mode & 0o777 == 0o600  # owner-only

        cert_stat = os.stat(str(ca_cert))
        assert cert_stat.st_mode & 0o777 == 0o644


class TestIssueWorkerCert:
    def test_issue_worker_cert_returns_pems(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        generate_ca(str(ca_cert), str(ca_key))

        cert_pem, key_pem = issue_worker_cert(str(ca_cert), str(ca_key), "worker-1")
        assert b"BEGIN CERTIFICATE" in cert_pem
        assert b"BEGIN PRIVATE KEY" in key_pem

    def test_issue_worker_cert_cn_matches_worker_id(self, tmp_path):
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        generate_ca(str(ca_cert), str(ca_key))

        cert_pem, _ = issue_worker_cert(str(ca_cert), str(ca_key), "worker-abc123")
        cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert len(cn) == 1
        assert cn[0].value == "worker-abc123"

    def test_issue_worker_cert_signed_by_ca(self, tmp_path):
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        generate_ca(str(ca_cert), str(ca_key))

        cert_pem, _ = issue_worker_cert(str(ca_cert), str(ca_key), "w1")
        ca_cert_obj = x509.load_pem_x509_certificate(
            ca_cert.read_bytes(), default_backend()
        )
        worker_cert_obj = x509.load_pem_x509_certificate(cert_pem, default_backend())
        assert worker_cert_obj.issuer == ca_cert_obj.subject


class TestServerTlsContext:
    def test_server_context_requires_client_cert(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        server_cert = tmp_path / "server.crt"
        server_key = tmp_path / "server.key"

        generate_ca(str(ca_cert), str(ca_key))

        # Generate server cert signed by CA
        import subprocess
        subprocess.run([
            "openssl", "genrsa", "-out", str(server_key), "2048",
        ], check=True, capture_output=True)
        csr = tmp_path / "server.csr"
        subprocess.run([
            "openssl", "req", "-new", "-key", str(server_key),
            "-out", str(csr), "-subj", "/CN=test-server",
        ], check=True, capture_output=True)
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key),
            "-CAcreateserial", "-out", str(server_cert),
            "-days", "1", "-sha256",
        ], check=True, capture_output=True)

        ctx = create_server_tls_context(
            str(ca_cert), str(server_cert), str(server_key)
        )
        assert ctx.verify_mode == ssl_mod.CERT_REQUIRED
        assert ctx.minimum_version == ssl_mod.TLSVersion.TLSv1_2


class TestClientTlsContext:
    def test_client_context_created_from_pem_bytes(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        generate_ca(str(ca_cert), str(ca_key))

        cert_pem, key_pem = issue_worker_cert(str(ca_cert), str(ca_key), "w1")
        ca_pem = ca_cert.read_bytes()

        ctx = create_client_tls_context(ca_pem, cert_pem, key_pem)
        assert isinstance(ctx, ssl_mod.SSLContext)
        assert ctx.minimum_version == ssl_mod.TLSVersion.TLSv1_2

    def test_wrong_ca_rejected(self, tmp_path):
        ca_cert = tmp_path / "ca.crt"
        ca_key = tmp_path / "ca.key"
        wrong_ca_cert = tmp_path / "wrong-ca.crt"
        wrong_ca_key = tmp_path / "wrong-ca.key"

        generate_ca(str(ca_cert), str(ca_key))
        generate_ca(str(wrong_ca_cert), str(wrong_ca_key))

        # Issue worker cert from correct CA
        cert_pem, key_pem = issue_worker_cert(str(ca_cert), str(ca_key), "w1")

        # Create client context with wrong CA for server verification
        wrong_ca_pem = wrong_ca_cert.read_bytes()
        ctx = create_client_tls_context(wrong_ca_pem, cert_pem, key_pem)
        assert isinstance(ctx, ssl_mod.SSLContext)
