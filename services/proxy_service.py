"""Global outbound proxy helpers for upstream ChatGPT and CPA requests."""

from __future__ import annotations

import json
import time
from threading import Lock
from urllib.parse import urlparse

from curl_cffi.requests import Session

from services.config import DATA_DIR, config


class ProxySettingsStore:
    """Resolve global/account proxy settings and rotate proxy pools safely."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._pool_indexes: dict[str, int] = {}

    def select_proxy(self, value: object = "") -> str:
        """Return one normalized proxy URL from a proxy value/pool using round-robin."""
        pool = parse_proxy_pool(value)
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        key = "\n".join(pool)
        with self._lock:
            index = self._pool_indexes.get(key, 0)
            selected = pool[index % len(pool)]
            self._pool_indexes[key] = (index + 1) % len(pool)
            return selected

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
