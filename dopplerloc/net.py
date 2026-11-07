"""All internet downloads go through here, so certificate problems are handled in one place.

Python installed from python.org (macOS especially, sometimes Windows) can lack the root certificates, which gives
"CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate". We therefore build the TLS context from, in order:
  1. the file named by SSL_CERT_FILE (use this behind a company proxy / antivirus that inspects HTTPS),
  2. truststore  - the operating system's own certificate store (what your browser uses),
  3. certifi     - a bundled list of public root certificates,
  4. Python's default.
DOPPLERLOC_INSECURE_SSL=1 switches checking off (last resort; only reasonable for these public, non-secret downloads).
"""
import os, ssl, urllib.error, urllib.request

HELP = ("Python could not verify the security certificate of {host}. Usual fixes:\n"
        "  1. Run:  python -m pip install --upgrade certifi truststore   (then restart dopplerloc)\n"
        "  2. macOS with Python from python.org: open Finder > Applications > Python 3.x and double-click 'Install Certificates.command'.\n"
        "  3. On a work network, or with an antivirus that scans HTTPS: export your organisation's root certificate (.pem) and set the\n"
        "     environment variable SSL_CERT_FILE to its path.\n"
        "  4. Last resort: set DOPPLERLOC_INSECURE_SSL=1 (turns certificate checking off for dopplerloc's public downloads only).\n"
        "See SETUP_GUIDE.md, section 'Certificate error'.  Original error: {err}")


class NetError(RuntimeError):
    pass


_ctx = None


def context():
    global _ctx
    if _ctx is not None:
        return _ctx
    if os.environ.get("DOPPLERLOC_INSECURE_SSL") == "1":
        c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    elif os.environ.get("SSL_CERT_FILE") and os.path.exists(os.environ["SSL_CERT_FILE"]):
        c = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    else:
        c = None
        try:
            import truststore
            c = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            try:
                import certifi
                c = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                c = ssl.create_default_context()
    _ctx = c
    return c


def _is_cert_error(e):
    r = getattr(e, "reason", e)
    return isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e)


def urlopen(req, timeout=30):
    """urllib.request.urlopen with the context above and a readable error for certificate problems."""
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=context())
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ssl.SSLError) as e:
        if _is_cert_error(e):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            raise NetError(HELP.format(host=url.split("/")[2] if "//" in url else url, err=e)) from e
        raise


def retrieve(url, path, timeout=60):
    """Download url to path (like urlretrieve)."""
    with urlopen(urllib.request.Request(url, headers={"User-Agent": "dopplerloc"}), timeout=timeout) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
