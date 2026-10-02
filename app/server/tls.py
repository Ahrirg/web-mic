"""Locally generated TLS certificates for HTTPS on the LAN.

Mobile browsers only allow microphone capture (getUserMedia) in a secure
context: https:// or http://localhost. To use the microphone over Wi-Fi we
serve HTTPS with:
  * a local CA ("Phone Mic Router Local CA"), generated once and kept in
    ~/.config/phone-mic-router/tls/ca.pem with a private key readable only by you;
  * a server certificate signed by that CA whose SANs list every local IP,
    localhost and the hostname. It is regenerated whenever the IP set changes.

The user either accepts the browser's certificate warning once, or installs
ca.crt (served at /ca.crt) on the phone to remove the warning permanently.
Nothing here contacts the internet.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger(__name__)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class CertificateManager:
    def __init__(self, directory: Path):
        self.dir = directory
        self.ca_cert_path = directory / "ca.pem"
        self.ca_key_path = directory / "ca-key.pem"
        self.cert_path = directory / "server.pem"
        self.key_path = directory / "server-key.pem"
        self.meta_path = directory / "server.json"

    # ------------------------------------------------------------------
    def ensure(self, hosts: list[str]) -> bool:
        """Make sure a valid server certificate covering `hosts` exists. Returns True if (re)generated."""
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        ca_cert, ca_key = self._ensure_ca()
        wanted = sorted(set(hosts) | {"localhost", "127.0.0.1", "::1"})
        if self._server_cert_ok(wanted, ca_cert):
            return False
        self._make_server_cert(wanted, ca_cert, ca_key)
        return True

    def ca_der(self) -> bytes:
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        return cert.public_bytes(serialization.Encoding.DER)

    def ca_fingerprint(self) -> str:
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        fp = cert.fingerprint(hashes.SHA256()).hex().upper()
        return ":".join(fp[i : i + 2] for i in range(0, len(fp), 2))

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    def san_hosts(self) -> list[str]:
        try:
            return json.loads(self.meta_path.read_text()).get("hosts", [])
        except (OSError, ValueError):
            return []

    # ------------------------------------------------------------------
    def _ensure_ca(self):
        if self.ca_cert_path.exists() and self.ca_key_path.exists():
            try:
                cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
                key = serialization.load_pem_private_key(self.ca_key_path.read_bytes(), None)
                if cert.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30):
                    return cert, key
            except (ValueError, OSError):
                log.warning("Local CA is unreadable; generating a new one")
        log.info("Generating local certificate authority in %s", self.dir)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Phone Mic Router Local CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Phone Mic Router (local)"),
        ])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        _write_private(self.ca_key_path, _key_pem(key))
        self.ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert, key

    def _server_cert_ok(self, wanted: list[str], ca_cert) -> bool:
        if not (self.cert_path.exists() and self.key_path.exists()):
            return False
        if self.san_hosts() != wanted:
            return False
        try:
            cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        except ValueError:
            return False
        if cert.issuer != ca_cert.subject:
            return False
        return cert.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14)

    def _make_server_cert(self, hosts: list[str], ca_cert, ca_key) -> None:
        log.info("Generating HTTPS certificate for: %s", ", ".join(hosts))
        key = ec.generate_private_key(ec.SECP256R1())
        sans: list[x509.GeneralName] = []
        for h in hosts:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(h)))
            except ValueError:
                sans.append(x509.DNSName(h))
        now = dt.datetime.now(dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Phone Mic Router")]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            # Browsers reject leaf certificates valid for more than 398 days.
            .not_valid_after(now + dt.timedelta(days=390))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=True,
                encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_private(self.key_path, _key_pem(key))
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.meta_path.write_text(json.dumps({"hosts": hosts}))
