"""SSRF 闸门单测(OPT-27:空间自配 base_url 属用户可控出站地址)。

只测判定函数本身(纯函数,不打网络):域名解析路径用 monkeypatch 桩掉,
IP 字面量与按名字拦的元数据域名走真实判定。
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.core.ssrf import assert_external_http_url, is_internal_address


def test_internal_address_classes() -> None:
    for value in (
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # 云厂商元数据
        "0.0.0.0",
        "::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4 混写也要判成内网
    ):
        assert is_internal_address(value), value
    for value in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert not is_internal_address(value), value
    assert not is_internal_address("not-an-ip")


def test_public_url_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.ssrf._resolve", lambda host: ["93.184.216.34"])
    assert_external_http_url("https://api.deepseek.com/v1")


def test_ip_literal_and_name_rules() -> None:
    assert_external_http_url("https://8.8.8.8/v1")  # 公网 IP 字面量放行
    for bad in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/x",
        "http://printer.local:9100/",
        "ftp://10.0.0.1/x",  # 非 http(s) 形态
        "https:///v1",  # 没有主机名
    ):
        with pytest.raises(AppError) as exc_info:
            assert_external_http_url(bad)
        assert exc_info.value.http_status == 422
        assert "不安全" in exc_info.value.message


def test_domain_resolving_internal_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.core.ssrf._resolve", lambda host: ["93.184.216.34", "10.0.0.9"])
    with pytest.raises(AppError) as exc_info:
        assert_external_http_url("https://gateway.corp.example/v1")
    assert "10.0.0.9" in exc_info.value.message


def test_unresolvable_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """解不动 = 连不上,不该在入库时打死(离线环境与测试环境都要它通过)。"""

    def raise_gaierror(host: str, port: object) -> list:
        raise OSError(f"no such host: {host}")

    monkeypatch.setattr("app.core.ssrf.socket.getaddrinfo", raise_gaierror)
    assert_external_http_url("https://api.example.com/v1")
