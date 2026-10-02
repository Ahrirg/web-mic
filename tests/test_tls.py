import ipaddress
import ssl

from cryptography import x509

from app.server.tls import CertificateManager


def sans(path):
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in ext.get_values_for_type(x509.IPAddress)} | set(ext.get_values_for_type(x509.DNSName))


def test_generates_ca_and_server_cert(tmp_path):
    m = CertificateManager(tmp_path / "tls")
    assert m.ensure(["192.168.1.50", "myhost"]) is True
    names = sans(m.cert_path)
    assert {"192.168.1.50", "127.0.0.1", "::1", "localhost", "myhost"} <= names
    assert (m.key_path.stat().st_mode & 0o777) == 0o600
    assert (m.ca_key_path.stat().st_mode & 0o777) == 0o600
    ctx = m.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert len(m.ca_der()) > 100 and m.ca_fingerprint().count(":") == 31


def test_server_cert_verifies_against_ca(tmp_path):
    m = CertificateManager(tmp_path / "tls")
    m.ensure(["10.0.0.5"])
    ca = x509.load_pem_x509_certificate(m.ca_cert_path.read_bytes())
    leaf = x509.load_pem_x509_certificate(m.cert_path.read_bytes())
    leaf.verify_directly_issued_by(ca)
    assert ipaddress.ip_address("10.0.0.5") in leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)


def test_reuse_and_regenerate_on_ip_change(tmp_path):
    m = CertificateManager(tmp_path / "tls")
    m.ensure(["192.168.1.50"])
    ca_before = m.ca_cert_path.read_bytes()
    assert m.ensure(["192.168.1.50"]) is False
    assert m.ensure(["192.168.1.60"]) is True
    assert "192.168.1.60" in sans(m.cert_path)
    assert m.ca_cert_path.read_bytes() == ca_before  # CA stays stable, phones keep trusting it
