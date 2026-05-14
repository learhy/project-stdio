"""Mutual TLS helpers: CA generation, worker cert issuance, TLS context creation.

Replaces the token-only auth model from Bundle 4.1 with full mTLS.
"""

from __future__ import annotations

import datetime
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

_CA_VALIDITY_YEARS = 10
_WORKER_CERT_VALIDITY_MINUTES = 20
_KEY_SIZE = 4096


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def generate_ca(ca_cert_path: str, ca_key_path: str) -> tuple[bytes, bytes]:
    """Generate a self-signed CA certificate and private key (idempotent).

    Returns (ca_cert_pem, ca_key_pem). If files already exist, reads and
    returns their contents instead of regenerating.
    """
    if os.path.exists(ca_cert_path) and os.path.exists(ca_key_path):
        return Path(ca_cert_path).read_bytes(), Path(ca_key_path).read_bytes()

    _ensure_dir(ca_cert_path)
    _ensure_dir(ca_key_path)

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=_KEY_SIZE, backend=default_backend()
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Studio Orchestrator CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Studio"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CA_VALIDITY_YEARS * 365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                key_cert_sign=True, crl_sign=True, digital_signature=False,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .sign(private_key, hashes.SHA256(), default_backend())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    Path(ca_cert_path).write_bytes(cert_pem)
    Path(ca_cert_path).chmod(0o644)
    Path(ca_key_path).write_bytes(key_pem)
    Path(ca_key_path).chmod(0o600)

    return cert_pem, key_pem


def issue_worker_cert(
    ca_cert_path: str, ca_key_path: str, worker_id: str
) -> tuple[bytes, bytes]:
    """Issue a short-lived worker certificate signed by the CA.

    Returns (cert_pem, key_pem). The cert CN is set to the worker_id
    for identity verification during mTLS handshake.
    """
    ca_cert_pem = Path(ca_cert_path).read_bytes()
    ca_key_pem = Path(ca_key_path).read_bytes()

    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem, default_backend())
    ca_key = serialization.load_pem_private_key(ca_key_pem, None, default_backend())

    worker_key = rsa.generate_private_key(
        public_exponent=65537, key_size=_KEY_SIZE, backend=default_backend()
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, worker_id),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Studio Worker"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(worker_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(minutes=_WORKER_CERT_VALIDITY_MINUTES))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = worker_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return cert_pem, key_pem


def create_server_tls_context(
    ca_cert_path: str, server_cert_path: str, server_key_path: str
) -> ssl.SSLContext:
    """Create a server-side TLS context that requires and verifies client certs.

    The server presents its own cert and requires clients to present a
    certificate signed by the CA.
    """
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=ca_cert_path)
    ctx.load_cert_chain(server_cert_path, server_key_path)
    return ctx


def create_client_tls_context(
    ca_cert_pem: bytes, worker_cert_pem: bytes, worker_key_pem: bytes
) -> ssl.SSLContext:
    """Create a client-side TLS context that presents a worker cert and verifies the server.

    The client verifies the server's certificate against the CA and presents
    its own worker certificate for mutual authentication.
    """
    # Load CA for server verification
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem, default_backend())

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=ca_cert_pem.decode())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False  # CN verification is done via cert subject, not hostname

    # Load worker cert + key from memory (PEM bytes)
    worker_cert = x509.load_pem_x509_certificate(worker_cert_pem, default_backend())
    worker_key = serialization.load_pem_private_key(worker_key_pem, None, default_backend())

    # Write temp files for ssl.load_cert_chain (stdlib SSL only reads from files)
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="studio-mtls-")
    cert_file = os.path.join(tmpdir, "worker.crt")
    key_file = os.path.join(tmpdir, "worker.key")

    try:
        Path(cert_file).write_bytes(worker_cert_pem)
        Path(key_file).write_bytes(worker_key_pem)
        Path(key_file).chmod(0o600)
        ctx.load_cert_chain(cert_file, key_file)
    finally:
        # Clean up temp files immediately — cert material is already loaded into the context
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return ctx
