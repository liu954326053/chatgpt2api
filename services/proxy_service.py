"""Global outbound proxy helpers for upstream ChatGPT and CPA requests."""

from __future__ import annotations

import json
import time
from threading import Condition, Lock
from urllib.parse import urlparse

from curl_cffi.requests import Session

from services.config import DATA_DIR, config


class ProxyLease:
    """A leased proxy slot from a proxy pool."""

    def __init__(self, store: "ProxySettingsStore", pool_key: str, proxy: str) -> None:
        self._store = store
        self._pool_key = pool_key
        self.proxy = proxy
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._store.release_proxy(self)

    def __enter__(self) -> "ProxyLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class ProxySettingsStore:
    """Resolve global/account proxy settings and rotate proxy pools safely."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._pool_indexes: dict[str, int] = {}
        self._busy_proxies: dict[str, set[str]] = {}

    def select_proxy(self, value: object = "") -> str:
        """Return one normalized proxy URL from a proxy value/pool using round-robin."""
        pool = parse_proxy_pool(value)
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        key = "select\n" + "\n".join(pool)
        with self._lock:
            index = self._pool_indexes.get(key, 0)
            selected = pool[index % len(pool)]
            self._pool_indexes[key] = (index + 1) % len(pool)
            return selected

    def acquire_proxy(
        self,
        value: object = "",
        *,
        scope: str = "default",
        wait: bool = True,
        timeout: float | None = None,
        on_wait=None,
    ) -> ProxyLease:
        """Lease one idle proxy from a pool.

        Unlike select_proxy(), a leased proxy is marked busy until release_proxy()
        is called, so concurrent long-running flows (registration) do not receive
        the same proxy at the same time.
        """
        pool = parse_proxy_pool(value)
        if not pool:
            return ProxyLease(self, "", "")
        key = f"lease:{scope}\n" + "\n".join(pool)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        wait_notified = False
        with self._condition:
            while True:
                busy = self._busy_proxies.setdefault(key, set())
                if len(busy) < len(pool):
                    start_index = self._pool_indexes.get(key, 0)
                    for offset in range(len(pool)):
                        index = (start_index + offset) % len(pool)
                        selected = pool[index]
                        if selected in busy:
                            continue
                        busy.add(selected)
                        self._pool_indexes[key] = (index + 1) % len(pool)
                        return ProxyLease(self, key, selected)
                if not wait:
                    raise TimeoutError("proxy pool exhausted")
                if not wait_notified and on_wait:
                    try:
                        on_wait(len(pool))
                    except Exception:
                        pass
                    wait_notified = True
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("proxy pool acquire timeout")
                self._condition.wait(remaining)

    def acquire_registration_proxy(self, register_proxy: object = "", **kwargs) -> ProxyLease:
        """Lease a proxy for registration.

        Registration proxy takes precedence. When it is empty, fall back to the
        global proxy pool. If both are empty, return a direct/no-proxy lease.
        """
        proxy_value = str(register_proxy or "").strip() or config.get_proxy_settings()
        return self.acquire_proxy(proxy_value, scope="register", **kwargs)

    def release_proxy(self, lease: ProxyLease | None) -> None:
        if lease is None or not lease._pool_key or not lease.proxy:
            return
        with self._condition:
            busy = self._busy_proxies.get(lease._pool_key)
            if busy is not None:
                busy.discard(lease.proxy)
                if not busy:
                    self._busy_proxies.pop(lease._pool_key, None)
            self._condition.notify()

    def build_session_kwargs(self, account: dict | None = None, proxy: str = "", **session_kwargs) -> dict[str, object]:
        account_proxy = str((account or {}).get("proxy") or "").strip()
        proxy_value = proxy or account_proxy or config.get_proxy_settings() or get_register_proxy_settings()
        selected_proxy = self.select_proxy(proxy_value)
        if selected_proxy:
            session_kwargs["proxy"] = selected_proxy
        return session_kwargs


def _clean(value: object) -> str:
    return str(value or "").strip()


def _split_proxy_entries(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        entries = [_clean(item) for item in value]
    else:
        raw = str(value or "")
        # 主配置使用“一行一个”。兼容少量逗号/分号分隔的历史手填值，但不按普通空格拆，避免破坏密码。
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").replace(";", "\n")
        if "\n" not in normalized and "," in normalized:
            normalized = normalized.replace(",", "\n")
        entries = [_clean(line) for line in normalized.split("\n")]
    return [item for item in entries if item and not item.startswith("#")]


def normalize_proxy_url(url: object) -> str:
    candidate = _clean(url)
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = "http://" + candidate
    return candidate


def parse_proxy_pool(value: object) -> list[str]:
    pool: list[str] = []
    seen: set[str] = set()
    for entry in _split_proxy_entries(value):
        proxy = normalize_proxy_url(entry)
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        pool.append(proxy)
    return pool


def get_register_proxy_settings() -> str:
    """Return the proxy configured for the registration worker as a safe fallback.

    The project has two proxy entry points in the UI: the global upstream proxy
    and the registration proxy. Users commonly configure only the registration
    proxy because it is required for signup. ChatGPT quota refresh and image
    generation hit the same upstream domains, so falling back to the registration
    proxy avoids direct connections timing out while still letting an explicit
    per-account or global proxy take precedence.
    """
    try:
        path = DATA_DIR / "register.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    proxy = str(data.get("proxy") or "").strip()
    if proxy:
        return proxy
    mail = data.get("mail")
    if isinstance(mail, dict):
        return str(mail.get("proxy") or "").strip()
    return ""


def _is_valid_proxy_url(url: str) -> bool:
    parsed = urlparse(normalize_proxy_url(url))
    return parsed.scheme in {"http", "https", "socks5", "socks5h"} and bool(parsed.netloc)


def _mask_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    auth = parsed.username or ""
    if parsed.password:
        auth += ":****"
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return parsed._replace(netloc=f"{auth}@{host}").geturl()


def _test_single_proxy(url: str, *, timeout: float) -> dict:
    candidate = normalize_proxy_url(url)
    started = time.perf_counter()
    if not candidate:
        return {"ok": False, "url": "", "status": 0, "latency_ms": 0, "error": "proxy url is required"}
    if not _is_valid_proxy_url(candidate):
        return {"ok": False, "url": candidate, "status": 0, "latency_ms": 0, "error": "invalid proxy url"}
    session = Session(impersonate="edge101", verify=True, proxy=candidate)
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"user-agent": "Mozilla/5.0 (chatgpt2api proxy test)"},
            timeout=timeout,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": response.status_code < 500,
            "url": _mask_proxy_url(candidate),
            "status": int(response.status_code),
            "latency_ms": latency_ms,
            "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "url": _mask_proxy_url(candidate),
            "status": 0,
            "latency_ms": latency_ms,
            "error": str(exc) or exc.__class__.__name__,
        }
    finally:
        session.close()


def test_proxy(url: str, *, timeout: float = 15.0) -> dict:
    pool = parse_proxy_pool(url)
    if not pool:
        return {"ok": False, "status": 0, "latency_ms": 0, "error": "proxy url is required", "total": 0, "passed": 0, "failed": 0, "items": []}
    items = [_test_single_proxy(item, timeout=timeout) for item in pool]
    passed = sum(1 for item in items if item.get("ok"))
    failed = len(items) - passed
    if len(items) == 1:
        item = dict(items[0])
        item.update({"total": 1, "passed": passed, "failed": failed, "items": items})
        return item
    return {
        "ok": failed == 0,
        "status": int(items[0].get("status") or 0),
        "latency_ms": sum(int(item.get("latency_ms") or 0) for item in items),
        "error": None if failed == 0 else f"{failed}/{len(items)} proxies failed",
        "total": len(items),
        "passed": passed,
        "failed": failed,
        "items": items,
    }


proxy_settings = ProxySettingsStore()
