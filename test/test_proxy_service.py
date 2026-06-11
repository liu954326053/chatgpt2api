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


def test_proxy_lease_uses_idle_proxies_without_reuse_until_release():
    store = ProxySettingsStore()
    proxies = "proxy-a.example.com:1001\nproxy-b.example.com:1002"

    lease_a = store.acquire_proxy(proxies, scope="register")
    lease_b = store.acquire_proxy(proxies, scope="register")

    assert lease_a.proxy == "http://proxy-a.example.com:1001"
    assert lease_b.proxy == "http://proxy-b.example.com:1002"

    lease_a.release()
    lease_c = store.acquire_proxy(proxies, scope="register", wait=False)
    assert lease_c.proxy == "http://proxy-a.example.com:1001"

    lease_b.release()
    lease_c.release()


def test_registration_proxy_falls_back_to_global_proxy(monkeypatch):
    store = ProxySettingsStore()
    monkeypatch.setattr("services.proxy_service.config.get_proxy_settings", lambda: "global-a:1\nglobal-b:2")

    lease = store.acquire_registration_proxy("")

    assert lease.proxy == "http://global-a:1"
    lease.release()
