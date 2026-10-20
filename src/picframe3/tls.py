"""Self-signed certificates for serving the web interface over https://.

Shared by the ``picframe3 enable-https`` CLI command and the "Enable HTTPS"
button on the settings page's Web interface card, so the two have exactly
one idea of where a certificate lives and how it is made -- a fix to one
is a fix to both.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass

from .config import Config


@dataclass
class TlsResult:
    ok: bool
    cert_path: str = ""
    key_path: str = ""
    hostname: str = ""
    lan_ip: str | None = None
    wrote: bool = False          # False when an existing cert was reused
    error: str = ""


def detect_lan_ip() -> str | None:
    """The address this Pi would use to reach the rest of the network --
    found without actually sending anything (a UDP "connect" just asks the
    kernel which local address would carry that traffic), which is also why
    it works with no internet access and 10.255.255.255 need not exist."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


def cert_paths(config: Config) -> tuple[str, str]:
    base_dir = os.path.dirname(os.path.abspath(
        config.source_path or os.path.expanduser("~/.config/picframe3/config.yaml")))
    tls_dir = os.path.join(base_dir, "tls")
    return os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")


def enable_https(config: Config, force: bool = False) -> TlsResult:
    """Write a self-signed certificate (unless one is already there and
    ``force`` is not set) and point ``config.http.tls_cert``/``tls_key`` at
    it. Saves the config. Does not restart the frame -- the caller decides
    whether that happens via the usual restart-required flow.
    """
    cert_path, key_path = cert_paths(config)
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    hostname = socket.gethostname()
    lan_ip = detect_lan_ip()
    wrote = False

    if os.path.exists(cert_path) and os.path.exists(key_path) and not force:
        pass
    else:
        names = ["DNS:localhost", f"DNS:{hostname}", "IP:127.0.0.1"]
        if lan_ip:
            names.append(f"IP:{lan_ip}")
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key_path, "-out", cert_path, "-days", "3650",
                 "-subj", f"/CN={hostname}",
                 "-addext", f"subjectAltName={','.join(names)}"],
                check=True, capture_output=True, text=True)
        except FileNotFoundError:
            return TlsResult(ok=False, error=(
                "openssl is not installed. Install it (sudo apt-get install "
                "openssl) and try again -- or point http.tls_cert and "
                "http.tls_key at a certificate of your own."))
        except subprocess.CalledProcessError as exc:
            return TlsResult(ok=False, error=f"openssl could not write a certificate: {exc.stderr}")
        os.chmod(key_path, 0o600)
        wrote = True

    config.http.tls_cert = cert_path
    config.http.tls_key = key_path
    config.save()
    return TlsResult(ok=True, cert_path=cert_path, key_path=key_path,
                     hostname=hostname, lan_ip=lan_ip, wrote=wrote)
