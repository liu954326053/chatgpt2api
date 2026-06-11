from services.proxy_service import ProxySettingsStore, parse_proxy_pool


def test_parse_proxy_pool_multiline_and_default_scheme():
    pool = parse_proxy_pool(
        """
        user-a:pass@proxy.example.com:1463
        http://user-b:pass@proxy.example.com:1461
        socks5://127.0.0.1:7890
        user-a:pass@proxy.example.com:1463
        # comment
        """
    )
    assert pool == [
        "http://user-a:pass@proxy.example.com:1463",
        "http://user-b:pass@proxy.example.com:1461",
        "socks5://127.0.0.1:7890",
    ]


def test_proxy_pool_round_robin():
    store = ProxySettingsStore()
    proxies = """
    proxy-a.example.com:1001
    proxy-b.example.com:1002
    proxy-c.example.com:1003
    """
    assert store.select_proxy(proxies) == "http://proxy-a.example.com:1001"
    assert store.select_proxy(proxies) == "http://proxy-b.example.com:1002"
    assert store.select_proxy(proxies) == "http://proxy-c.example.com:1003"
    assert store.select_proxy(proxies) == "http://proxy-a.example.com:1001"


def test_account_proxy_overrides_global_pool(monkeypatch):
    store = ProxySettingsStore()
    monkeypatch.setattr("services.proxy_service.config.get_proxy_settings", lambda: "global-a:1\nglobal-b:2")
    kwargs = store.build_session_kwargs(account={"proxy": "account-a:3\naccount-b:4"}, verify=True)
    assert kwargs["verify"] is True
    assert kwargs["proxy"] == "http://account-a:3"
    kwargs = store.build_session_kwargs(account={"proxy": "account-a:3\naccount-b:4"}, verify=True)
    assert kwargs["proxy"] == "http://account-b:4"
