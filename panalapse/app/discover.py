"""Find Panasonic AW cameras by asking each address on a subnet for its model.

Panasonic's own discovery (Easy IP Setup) is an undocumented broadcast protocol,
and broadcasts rarely leave an add-on's container. Instead every address gets one
short "QID" request, the same one Detect uses; AW cameras answer "OID:<model>".
Cameras that need a login answer 401 and are listed as "login needed" (or, if a
user name and password were given, asked again with them)."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import time

import httpx

log = logging.getLogger("panalapse.discover")
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
SCAN_PORT = int(os.environ.get("PANALAPSE_SCAN_PORT", "80"))   # only changed for testing
MAX_ADDRESSES = 1024          # per scan, so a typo like /16 can't run for ages
CONCURRENCY = 48
TIMEOUT = httpx.Timeout(2.0, connect=1.2)
# Home Assistant's own internal networks: never worth scanning.
INTERNAL = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "172.17.0.0/16", "127.0.0.0/8")]


def _usable(net: ipaddress.IPv4Network) -> bool:
    return (net.version == 4 and not net.is_loopback and not net.is_link_local
            and not any(net.subnet_of(i) for i in INTERNAL if i.version == 4))


async def host_subnets() -> list[str]:
    """Subnets Home Assistant's host is on (via the Supervisor), else this machine's."""
    found: list[ipaddress.IPv4Interface] = []
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        try:
            async with httpx.AsyncClient(timeout=5) as cl:
                r = await cl.get(f"{SUPERVISOR_URL}/network/info",
                                 headers={"Authorization": f"Bearer {token}"})
            for itf in ((r.json() or {}).get("data") or {}).get("interfaces") or []:
                if itf.get("enabled") is False:
                    continue
                for a in ((itf.get("ipv4") or {}).get("address") or []):
                    try:
                        found.append(ipaddress.ip_interface(a))
                    except ValueError:
                        pass
        except (httpx.HTTPError, ValueError) as e:
            log.debug("network/info: %s", e)
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))      # no packet is sent
            found.append(ipaddress.ip_interface(s.getsockname()[0] + "/24"))
            s.close()
        except OSError:
            pass
    out = []
    for itf in found:
        if itf.version != 4:
            continue
        net = itf.network if itf.network.prefixlen >= 24 else \
            ipaddress.ip_interface(f"{itf.ip}/24").network
        if _usable(net) and str(net) not in out:
            out.append(str(net))
    return out


def parse_targets(text: str) -> list[str]:
    """'192.168.1.0/24, 10.0.0.5, 10.0.1.10-20' -> list of addresses."""
    addrs: list[str] = []
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and "/" not in part:
            start, end = part.split("-", 1)
            a = ipaddress.ip_address(start.strip())
            end = end.strip()
            b = ipaddress.ip_address(end if "." in end else str(a).rsplit(".", 1)[0] + "." + end)
            if int(b) < int(a):
                raise ValueError(f"{part}: range goes backwards")
            if int(b) - int(a) >= MAX_ADDRESSES:
                raise ValueError(f"{part}: too many addresses")
            addrs += [str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1)]
        else:
            net = ipaddress.ip_network(part, strict=False)
            if net.version != 4:
                raise ValueError(f"{part}: only IPv4 is supported")
            if net.num_addresses > MAX_ADDRESSES:
                raise ValueError(f"{part}: too big, scan at most a /22 ({MAX_ADDRESSES} addresses)")
            addrs += [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
        if len(addrs) > MAX_ADDRESSES:
            raise ValueError(f"Too many addresses, at most {MAX_ADDRESSES} per scan")
    seen = set()
    return [a for a in addrs if not (a in seen or seen.add(a))]


async def _ask(cl: httpx.AsyncClient, host: str, auth: dict | None) -> dict | None:
    url = f"http://{host}" + ("" if SCAN_PORT == 80 else f":{SCAN_PORT}") + "/cgi-bin/aw_cam?cmd=QID&res=1"
    try:
        r = await cl.get(url, headers={"Connection": "close"})
        if r.status_code == 401 and auth is not None:
            challenge = r.headers.get("www-authenticate", "").lower()
            r = await cl.get(url, headers={"Connection": "close"},
                             auth=auth["digest"] if "digest" in challenge else auth["basic"])
    except httpx.HTTPError:
        return None
    text = r.text.strip() if r.status_code == 200 else ""
    if text.startswith("OID:"):
        return {"host": host, "model": text[4:].strip() or "AW camera", "status": "ok"}
    if r.status_code == 401:
        realm = r.headers.get("www-authenticate", "")
        return {"host": host, "model": None, "status": "login",
                "detail": ("Asks for a login, and that user name and password didn't work" if auth else
                           "Asks for a login (probably a camera, or another device with a web login)"),
                "realm": realm[:80]}
    return None


async def scan(addresses: list[str], username: str = "", password: str = "",
               skip: set[str] | None = None) -> dict:
    t0 = time.monotonic()
    auth = ({"basic": httpx.BasicAuth(username, password),
             "digest": httpx.DigestAuth(username, password)} if username else None)
    sem = asyncio.Semaphore(CONCURRENCY)
    skip = skip or set()
    found: list[dict] = []
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=0)

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits) as cl:
        async def one(h: str):
            if h in skip:
                return
            async with sem:
                res = await _ask(cl, h, auth)
            if res:
                found.append(res)
        await asyncio.gather(*(one(h) for h in addresses))

    found.sort(key=lambda d: ipaddress.ip_address(d["host"]))
    return {"found": found, "scanned": len(addresses) - len(skip & set(addresses)),
            "seconds": round(time.monotonic() - t0, 1)}
