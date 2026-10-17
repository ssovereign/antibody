"""Restricted public HTTPS fetch adapter.

Enforces: https only, optional host allowlist, DNS resolution with rejection
of private / loopback / link-local / reserved / multicast addresses on every
redirect hop, a bounded redirect count, body size limit, content-type
allowlist, and no credentials or cookies. Records URL, redirects and hash.

Known limit (documented): resolution is checked immediately before the
request; a resolver that answers differently between the check and connect
(DNS rebinding) is mitigated only by the short window. A production
deployment should pin the connection to the vetted address.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from ..canonical import content_ref
from .base import ResourceViolation, ToolError

ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "text/markdown", "application/xhtml+xml")


def _default_resolver(host: str) -> list[str]:
    return list({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})


def is_public_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped is not None and not is_public_address(str(ip.ipv4_mapped))))


def check_url(url: str, allowed_hosts: list[str] | None, resolver=_default_resolver) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "only https URLs are permitted")
    if parts.username or parts.password:
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "credentials in URLs are not permitted")
    host = (parts.hostname or "").lower()
    if not host:
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "URL has no host")
    if parts.port not in (None, 443):
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "only port 443 is permitted")
    if allowed_hosts and host not in [h.lower() for h in allowed_hosts]:
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", f"host {host} is not in the allowed host list")
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            addrs = resolver(host)
        except OSError as e:
            raise ToolError("E_DNS", "DNSError", f"could not resolve {host}") from e
    if not addrs or not all(is_public_address(a) for a in addrs):
        raise ResourceViolation("NETWORK_NOT_AUTHORIZED", f"{host} resolves to a non-public address")
    return host


class FetchExecutor:
    def __init__(self, max_bytes: int, max_redirects: int, timeout: float, resolver=_default_resolver, transport=None):
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout = timeout
        self.resolver = resolver
        self.transport = transport  # httpx transport for tests

    def fetch(self, url: str, allowed_hosts: list[str] | None) -> dict:
        import httpx

        redirects = []
        current = url
        with httpx.Client(follow_redirects=False, timeout=self.timeout, transport=self.transport,
                          headers={"User-Agent": "ANTIBODY-fetch/1", "Accept": ", ".join(ALLOWED_CONTENT_TYPES)}) as client:
            for hop in range(self.max_redirects + 1):
                check_url(current, allowed_hosts, self.resolver)
                with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            raise ToolError("E_REDIRECT", "RedirectError", "redirect without location")
                        nxt = urljoin(current, loc)
                        redirects.append({"from": current, "to": nxt, "status": resp.status_code})
                        current = nxt
                        continue
                    if resp.status_code >= 400:
                        raise ToolError("E_HTTP", "HTTPError", f"HTTP {resp.status_code}", {"status": resp.status_code})
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        raise ToolError("E_CONTENT_TYPE", "ContentTypeError", f"content type {ctype or 'unknown'} not supported")
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > self.max_bytes:
                            raise ToolError("E_TOO_LARGE", "BodyTooLarge", f"body exceeds {self.max_bytes} bytes")
                    return {"status": "ok", "url": url, "final_url": current, "redirects": redirects,
                            "content_type": ctype, "bytes": len(body), "sha256": content_ref(body), "body": body}
        raise ToolError("E_REDIRECT_LIMIT", "RedirectLimit", f"more than {self.max_redirects} redirects")
