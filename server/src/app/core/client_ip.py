"""客户端 IP 解析(OPT-12 / Go-Live 翻转项 #8):仅对显式可信代理解析 X-Forwarded-For。

fail-closed:`APP_TRUSTED_PROXIES` 未配置(默认)时不信任任何代理,永远取直连地址;
配置后也只对"直连对端 ∈ 可信网段"的请求解析 XFF,防任意客户端伪造头污染审计与登录 IP。

XFF 语义:各跳依次追加,左侧最接近客户端、右侧最接近本服务。只有可信链上的头才可采;
自右向左剥掉可信网段内的跳,第一个不可信地址即客户端(反向代理协议的标准剥法)。
"""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

TrustedNetwork = IPv4Network | IPv6Network

_IP = IPv4Address | IPv6Address


def parse_trusted_proxies(raw: str) -> list[TrustedNetwork]:
    """逗号分隔 CIDR/单 IP → 网段列表;非法项直接抛错(启动/测试期暴露,不在请求路径兜底)。"""
    networks: list[TrustedNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        networks.append(ip_network(item, strict=False))
    return networks


def _in_trusted(ip: _IP, networks: list[TrustedNetwork]) -> bool:
    return any(ip in net for net in networks)


def resolve_client_ip(peer: str, headers: Mapping[str, str], trusted_proxies: str) -> str:
    """本次请求的客户端 IP:可信代理后取 XFF 剥链结果,否则取直连地址。"""
    networks = parse_trusted_proxies(trusted_proxies)
    if not networks:
        return peer
    try:
        peer_ip = ip_address(peer)
    except ValueError:
        return peer
    if not _in_trusted(peer_ip, networks):
        return peer

    xff = headers.get("x-forwarded-for") or ""
    chain = [item.strip() for item in xff.split(",") if item.strip()]
    if not chain:
        return peer
    for candidate in reversed(chain):
        try:
            ip = ip_address(candidate)
        except ValueError:
            # 链上出现非法值说明头被污染,整条不可采,回落直连地址
            return peer
        if not _in_trusted(ip, networks):
            return candidate
    return chain[0]
