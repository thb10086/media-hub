"""Client address resolution and CIDR matching."""

from __future__ import annotations

import ipaddress
from typing import Iterable, Union

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def parse_networks(cidrs: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(cidr.strip(), strict=False) for cidr in cidrs if cidr.strip())


def ip_in(ip: str | None, networks: Iterable[Network]) -> bool:
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


def client_ip(peer: str | None, forwarded_for: str | None, trusted: tuple[Network, ...]) -> str | None:
    """The caller's address, honouring X-Forwarded-For only from trusted proxies.

    Hops are read right to left and the first one that is not itself a trusted
    proxy is the client: anything further left was written by the client and
    cannot be believed.
    """
    if not peer or not forwarded_for or not ip_in(peer, trusted):
        return peer
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not ip_in(hop, trusted):
            return hop
    return hops[0] if hops else peer
