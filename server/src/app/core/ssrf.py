"""SSRF 防护:判定"用户可控的出站地址"是否指向内网(OPT-27 空间自配 base_url)。

为什么本轮必须有:多租户下**任何注册用户都能自助建空间、建完就是该空间 Owner**,空间层模型配置
把"服务端往哪儿发请求"交给了任何一个注册用户(见 `docs/架构设计.md` §8.1)。不判一下就
等于给全平台开了一个带响应分类的 SSRF 探头(【测试连接】把 auth/model_name/network 分得很清)。

判定口径:
- 只放行 http/https;主机名是 **IP 字面量**时直接判地址类别,是**域名**时解一次 DNS,
  解出的任一地址属于内网/回环/链路本地(含云元数据 169.254.169.254)/组播/保留即拒;
- DNS 解不出来**放行**:那种地址连不上,拦它只会把离线环境与测试环境一起打死,
  真正的连通性由【测试连接】负责;
- 已知元数据域名(`metadata.google.internal`、`*.internal`)直接按名字拦,不等 DNS。

已知残余风险(登记在 `docs/优化台账.md`):本模块判的是**写入时**的解析结果,攻击者若
掌握域名 TTL 做重绑定(写入时公网、请求时内网),这里挡不住;彻底方案要在传输层钉 IP
(自定义 httpx transport)。空间 Admin 已是"能改自己空间全部模型配置"的高权限身份,
重绑定要额外控制其域名,风险等级另计,不在本轮范围。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.errors import AppError, ErrorCode

# 云厂商/私网常用元数据与内网解析后缀:按名字直接拒,不给 DNS 机会
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".home.arpa")
_BLOCKED_HOSTS = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "instance"}
)


def is_internal_address(value: str) -> bool:
    """IP 字面量是否属于"不该由服务端去访问"的那一类。"""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 这类混写
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_external_http_url(base_url: str) -> None:
    """用户可控出站地址入库前的闸门;不安全则 422(消息说清哪一类地址不行)。"""
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise _denied("只允许 http(s) 地址")
    host = (parsed.hostname or "").strip(".").lower()
    if not host:
        raise _denied("地址里没有主机名")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise _denied(f"{host!r} 是内网/元数据域名")
    if _is_ip_literal(host):
        if is_internal_address(host):
            raise _denied(f"{host} 是内网/本机地址")
        return
    for address in _resolve(host):
        if is_internal_address(address):
            raise _denied(f"{host!r} 解析到内网/本机地址 {address}")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return True


def _resolve(host: str) -> list[str]:
    """域名 → 全部解析地址;解不动返回空列表(放行,连通性交给【测试连接】)。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError, socket.gaierror):
        return []
    return [str(info[4][0]) for info in infos]


def _denied(reason: str) -> AppError:
    return AppError(
        ErrorCode.VALIDATION,
        f"模型服务地址不安全:{reason}。生产环境不允许内网/本机地址(空间 Admin 自配的 base_url "
        "等同于让服务端去访问任意内网服务);确需内网自建网关,由运维显式开 "
        "APP_ALLOW_PRIVATE_MODEL_URLS=1。",
        http_status=422,
    )
