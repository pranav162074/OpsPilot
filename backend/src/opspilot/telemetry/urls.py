"""Integration URLs are chosen by workspace admins, and the server makes requests to them.
Without checks, an admin could point "Prometheus" at the server's own internal network or
the cloud metadata endpoint (server-side request forgery). In production every request's
target is resolved and must be a public address."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    pass


def parse_base_url(url: str) -> str:
    """Checks the URL's shape and returns it without a trailing slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("The URL must start with http:// or https://")
    if not parts.hostname:
        raise UnsafeUrl("The URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrl("Put credentials in the token field, not in the URL")
    if parts.query or parts.fragment:
        raise UnsafeUrl("The URL must not have a query string or fragment")
    return url.strip().rstrip("/")


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


async def ensure_public_host(url: str) -> None:
    """Raises UnsafeUrl unless every address the host resolves to is public."""
    host = urlsplit(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Can't resolve {host}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public(str(a)) for a in addresses):
        raise UnsafeUrl(f"{host} is a private or local address, which isn't allowed here")
