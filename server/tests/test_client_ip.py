"""可信代理 IP 解析单测(OPT-12 / Go-Live 翻转项 #8):fail-closed 默认 + 自右向左剥链。"""

from __future__ import annotations

import pytest

from app.core.client_ip import parse_trusted_proxies, resolve_client_ip


def _h(xff: str | None = None) -> dict[str, str]:
    return {} if xff is None else {"x-forwarded-for": xff}


def test_empty_trusted_config_always_returns_direct_peer():
    """默认未配置 = 不信任任何代理:XFF 头存在也不采。"""
    assert resolve_client_ip("198.51.100.7", _h("203.0.113.9"), "") == "198.51.100.7"


def test_untrusted_peer_ignores_spoofed_xff():
    """直连对端不在可信网段:客户端伪造 XFF 必须无效(审计防污染的核心用例)。"""
    assert (
        resolve_client_ip("198.51.100.7", _h("203.0.113.9"), "127.0.0.0/8,10.0.0.0/8")
        == "198.51.100.7"
    )


def test_trusted_peer_takes_rightmost_untrusted_hop():
    """可信反代后:链自右向左剥,第一个不可信地址即客户端。"""
    assert (
        resolve_client_ip("127.0.0.1", _h("203.0.113.9, 198.51.100.1"), "127.0.0.0/8")
        == "198.51.100.1"
    )


def test_multi_hop_trusted_intermediate_stripped():
    """中间跳也在可信网段时继续左剥。"""
    xff = "203.0.113.9, 198.51.100.1, 10.9.9.9"
    assert resolve_client_ip("127.0.0.1", _h(xff), "127.0.0.1,10.0.0.0/8") == "198.51.100.1"


def test_all_trusted_chain_returns_leftmost():
    """整条链都在可信网段(纯内网流量):取最左,不留空。"""
    xff = "10.1.1.1, 10.2.2.2"
    assert resolve_client_ip("127.0.0.1", _h(xff), "127.0.0.1,10.0.0.0/8") == "10.1.1.1"


def test_missing_or_empty_xff_falls_back_to_peer():
    trusted = "127.0.0.0/8"
    assert resolve_client_ip("127.0.0.1", _h(None), trusted) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", _h(""), trusted) == "127.0.0.1"


def test_malformed_chain_entry_poisons_whole_header():
    """链上出现非法值 = 头被污染,整条不可采,回落直连地址。"""
    assert resolve_client_ip("127.0.0.1", _h("203.0.113.9, evil"), "127.0.0.0/8") == "127.0.0.1"


def test_unparsable_peer_returns_as_is():
    """TestClient 等非网络对端(如 'testclient'):不崩溃,原样返回。"""
    assert resolve_client_ip("testclient", _h("203.0.113.9"), "127.0.0.0/8") == "testclient"
    assert resolve_client_ip("", {}, "127.0.0.0/8") == ""


def test_ipv6_trusted_proxy():
    xff = "2001:db8::5, fd00::2"
    assert resolve_client_ip("::1", _h(xff), "::1/128,fd00::/8") == "2001:db8::5"


def test_parse_trusted_proxies_accepts_cidr_and_plain_ip():
    nets = parse_trusted_proxies("127.0.0.1, 10.0.0.0/8,, ::1/128")
    assert len(nets) == 3


def test_parse_trusted_proxies_rejects_garbage():
    with pytest.raises(ValueError):
        parse_trusted_proxies("not-a-network")
