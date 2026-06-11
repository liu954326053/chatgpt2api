from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from services.account_service import account_service
from services.register import mail_provider
from services.proxy_service import proxy_settings

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 90,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="142", "Not?A_Brand";v="8", "Chromium";v="142"'
sec_ch_ua_full_version_list = '"Chromium";v="142.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="142.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None
proxy_authorize_lock = threading.Lock()
proxy_next_authorize_at: dict[str, float] = {}
proxy_authorize_min_interval = 3.5
proxy_flow_locks_guard = threading.Lock()
proxy_flow_locks: dict[str, threading.RLock] = {}
proxy_cooldown_lock = threading.Lock()
proxy_cooldown_until: dict[str, float] = {}
proxy_challenge_counts: dict[str, int] = {}

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


from utils.pkce import generate_pkce as _generate_pkce  # noqa: F401


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={str(getattr(resp, 'url', '') or '')[:300]}",
        f"status={getattr(resp, 'status_code', 'unknown')}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-mitigated", "cf-ray", "location", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(data, ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={str(getattr(resp, 'text', '') or '')[:limit]}")
    return ", ".join(parts)


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    status_code = int(getattr(resp, "status_code", 0) or 0)
    text = str(getattr(resp, "text", "") or "").lower()
    headers = getattr(resp, "headers", {}) or {}
    cf_mitigated = str(headers.get("cf-mitigated") or "").strip().lower()
    if cf_mitigated == "challenge":
        return True
    if "<title>just a moment" in text:
        return True
    if status_code not in (403, 429, 503):
        return False
    return "challenges.cloudflare.com" in text or "/cdn-cgi/challenge-platform/" in text or "cf-chl-" in text


def _is_retryable_network_error(error: str) -> bool:
    value = str(error or "").lower()
    return any(
        token in value
        for token in (
            "timed out",
            "timeout",
            "operation timed out",
            "connection reset",
            "connection aborted",
            "connection closed abruptly",
            "temporarily unavailable",
            "curl: (56)",
        )
    )


class MailboxRejectedForRetry(RuntimeError):
    """当前邮箱地址/域名被注册接口拒绝，允许同一 worker 换邮箱重试。"""


class MailCodeTimeoutForRetry(RuntimeError):
    """当前邮箱没有及时收到验证码，允许同一 worker 换邮箱重试。"""


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(config["mail"], username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(config["mail"], mailbox)


def _email_domain(email: str) -> str:
    return str(email or "").rsplit("@", 1)[-1].strip().lower() if "@" in str(email or "") else ""


def _is_domain_rejected_error(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
    message = str(data.get("message") or error_data.get("message") or "")
    code = str(data.get("code") or error_data.get("code") or "")
    return code in {"account_creation_failed", "unsupported_email"} or message in {"Failed to create account. Please try again.", "The email you provided is not supported."}


def _is_username_exists_error(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(data.get("code") or error_data.get("code") or "")
    return code in {"username_already_exists", "duplicate_email"}


from utils.sentinel import SentinelTokenGenerator, build_sentinel_token as _build_sentinel_token_tuple  # noqa: F401


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    last_error = ""
    for attempt in range(1, 4):
        try:
            sentinel_val, oai_sc_val = _build_sentinel_token_tuple(session, device_id, flow, user_agent=user_agent, sec_ch_ua=sec_ch_ua)
            if oai_sc_val:
                session.cookies.set("oai-sc", oai_sc_val, domain=".openai.com")
                session.cookies.set("oai-sc", oai_sc_val, domain=".auth.openai.com")
                session.cookies.set("oai-sc", oai_sc_val, domain="auth.openai.com")
            return sentinel_val
        except Exception as error:
            last_error = str(error)
            if attempt >= 3 or not _is_retryable_network_error(last_error):
                raise
            time.sleep(min(8.0, 1.5 * attempt) + random.uniform(0.2, 1.2))
    raise RuntimeError(last_error or "sentinel_token_failed")


def create_session(proxy: str = "") -> Any:
    kwargs = proxy_settings.build_session_kwargs(proxy=proxy, impersonate="chrome142", verify=False)
    session = requests.Session(**kwargs)
    setattr(session, "_selected_proxy", str(kwargs.get("proxy") or ""))
    return session


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    attempts = max(1, retry_attempts)
    timeout = kwargs.pop("timeout", default_timeout)
    for attempt in range(1, attempts + 1):
        try:
            return session.request(method.upper(), url, timeout=timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            if attempt < attempts:
                time.sleep(min(8.0, 1.5 * attempt) + random.uniform(0.2, 1.2))
    return None, last_error


def _throttle_proxy_authorize(session: requests.Session) -> None:
    proxy = str(getattr(session, "_selected_proxy", "") or "direct")
    with proxy_authorize_lock:
        now = time.monotonic()
        wait_for = max(0.0, proxy_next_authorize_at.get(proxy, 0.0) - now)
        proxy_next_authorize_at[proxy] = max(now, proxy_next_authorize_at.get(proxy, 0.0)) + proxy_authorize_min_interval
    if wait_for > 0:
        time.sleep(wait_for + random.uniform(0.1, 0.6))


def _proxy_key(session: requests.Session) -> str:
    return str(getattr(session, "_selected_proxy", "") or "direct")


def _proxy_flow_lock(session: requests.Session) -> threading.RLock:
    proxy = _proxy_key(session)
    with proxy_flow_locks_guard:
        lock = proxy_flow_locks.get(proxy)
        if lock is None:
            lock = threading.RLock()
            proxy_flow_locks[proxy] = lock
        return lock


def _wait_proxy_cooldown(session: requests.Session, index: int) -> None:
    proxy = _proxy_key(session)
    with proxy_cooldown_lock:
        wait_for = max(0.0, proxy_cooldown_until.get(proxy, 0.0) - time.monotonic())
    if wait_for > 0:
        step(index, f"代理 {proxy} 正在 Cloudflare 冷却，等待 {wait_for:.1f}s", "yellow")
        time.sleep(wait_for + random.uniform(0.5, 2.0))


def _mark_proxy_cloudflare(session: requests.Session, index: int, stage: str) -> None:
    proxy = _proxy_key(session)
    with proxy_cooldown_lock:
        count = min(6, proxy_challenge_counts.get(proxy, 0) + 1)
        proxy_challenge_counts[proxy] = count
        cooldown = min(300.0, 35.0 * count + random.uniform(5.0, 15.0))
        until = time.monotonic() + cooldown
        proxy_cooldown_until[proxy] = max(proxy_cooldown_until.get(proxy, 0.0), until)
    step(index, f"{stage} 触发 Cloudflare，代理 {proxy} 冷却 {cooldown:.1f}s", "yellow")


def _mark_proxy_success(session: requests.Session) -> None:
    proxy = _proxy_key(session)
    with proxy_cooldown_lock:
        proxy_challenge_counts.pop(proxy, None)
        proxy_cooldown_until.pop(proxy, None)


@contextmanager
def _serialized_proxy_flow(session: requests.Session, index: int):
    lock = _proxy_flow_lock(session)
    acquired_immediately = lock.acquire(blocking=False)
    if not acquired_immediately:
        step(index, f"同代理 {_proxy_key(session)} 已有注册链路在运行，等待代理闸门", "yellow")
        lock.acquire()
    try:
        if not acquired_immediately:
            step(index, "已进入代理闸门")
        yield
    finally:
        lock.release()


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def request_platform_oauth_token(session: requests.Session, code: str, code_verifier: str) -> dict | None:
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }
    resp, error = request_with_local_retry(
        session,
        "post",
        f"{auth_base}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=False,
        timeout=60,
    )
    if resp is None:
        raise RuntimeError(error or "oauth_token_network_error")
    if resp.status_code != 200:
        print(resp.text)
        return None
    return _response_json(resp)


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = proxy
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())
        self.current_email = ""
        self.code_verifier = ""
        self.platform_auth_code = ""

    def close(self) -> None:
        self.session.close()

    def _reset_flow_session(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        self.session = create_session(self.proxy)
        self.device_id = str(uuid.uuid4())
        self.current_email = ""
        self.code_verifier = ""
        self.platform_auth_code = ""

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _retry_after_network_error(self, error: str, index: int, stage: str, attempt: int) -> bool:
        if not _is_retryable_network_error(error) or attempt >= 3:
            return False
        step(index, f"{stage} 网络超时/连接异常，重新预热后重试 {attempt}/3: {error[:180]}", "yellow")
        time.sleep(3 * attempt + random.uniform(1.0, 3.0))
        self._prewarm_platform(index)
        return True

    def _prewarm_platform(self, index: int) -> None:
        _wait_proxy_cooldown(self.session, index)
        step(index, "开始预热 platform 会话")
        self.session.cookies.set("oai-did", self.device_id, domain=".openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        resp, error = request_with_local_retry(
            self.session,
            "get",
            f"{platform_base}/",
            headers=self._navigate_headers(),
            allow_redirects=True,
            verify=False,
        )
        if resp is None or resp.status_code != 200:
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(f"platform 预热被 Cloudflare challenge 拦截，{_response_debug_detail(resp)}")
            raise RuntimeError(error or f"platform_prewarm_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")
        step(index, "platform 会话预热完成")

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self._prewarm_platform(index)
        self.code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        authorize_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        resp = None
        error = ""
        for attempt in range(1, 4):
            _throttle_proxy_authorize(self.session)
            resp, error = request_with_local_retry(
                self.session,
                "get",
                authorize_url,
                headers=self._navigate_headers(f"{platform_base}/"),
                allow_redirects=False,
                verify=False,
                retry_attempts=1,
            )
            if resp is None and self._retry_after_network_error(error, index, "platform authorize", attempt):
                continue
            if not _is_cloudflare_challenge(resp):
                break
            if attempt < 3:
                _mark_proxy_cloudflare(self.session, index, "platform authorize")
                step(index, f"platform authorize 触发 Cloudflare challenge，重新预热后重试 {attempt}/3", "yellow")
                time.sleep(2 * attempt + random.uniform(0.5, 1.5))
                self._prewarm_platform(index)
        if resp is None or resp.status_code not in (200, 302):
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            if _is_cloudflare_challenge(resp):
                _mark_proxy_cloudflare(self.session, index, "platform authorize")
                debug = _response_debug_detail(resp)
                raise RuntimeError(f"被 Cloudflare challenge 拦截，纯协议会话缺少浏览器 JSD/cf_clearance，{debug}")
            debug = _response_debug_detail(resp)
            status = getattr(resp, "status_code", "unknown")
            raise RuntimeError(error or f"platform_authorize_http_{status}{detail}, {debug}")
        step(index, "platform authorize 完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        resp = None
        error = ""
        for attempt in range(1, 4):
            page_resp, page_error = request_with_local_retry(
                self.session,
                "get",
                f"{auth_base}/create-account/password",
                headers=self._navigate_headers(f"{auth_base}/create-account/password"),
                allow_redirects=True,
                verify=False,
                retry_attempts=1,
            )
            if page_resp is None or page_resp.status_code != 200:
                if _is_cloudflare_challenge(page_resp):
                    _mark_proxy_cloudflare(self.session, index, "注册密码页预取")
                    step(index, f"注册密码页预取遇到 Cloudflare challenge，尝试重新预热: {_response_debug_detail(page_resp)}", "yellow")
                    if attempt < 3:
                        time.sleep(2 * attempt + random.uniform(0.5, 1.5))
                        self._prewarm_platform(index)
                        continue
                else:
                    step(index, page_error or f"注册密码页预取失败，跳过页面预取继续提交: register_password_page_http_{getattr(page_resp, 'status_code', 'unknown')}, {_response_debug_detail(page_resp)}", "yellow")
            headers = self._json_headers(f"{auth_base}/create-account/password")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
            _throttle_proxy_authorize(self.session)
            resp, error = request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/user/register",
                json={"username": email, "password": password},
                headers=headers,
                verify=False,
                retry_attempts=1,
            )
            if resp is not None and resp.status_code == 200:
                step(index, "提交注册密码完成")
                return
            if resp is None and self._retry_after_network_error(error, index, "提交注册密码", attempt):
                continue
            if not _is_cloudflare_challenge(resp):
                break
            if attempt < 3:
                _mark_proxy_cloudflare(self.session, index, "提交注册密码")
                step(index, f"提交注册密码触发 Cloudflare challenge，重新预热后重试 {attempt}/3", "yellow")
                time.sleep(2 * attempt + random.uniform(0.5, 1.5))
                self._prewarm_platform(index)

        if resp is None or resp.status_code != 200:
            if _is_cloudflare_challenge(resp):
                _mark_proxy_cloudflare(self.session, index, "提交注册密码")
                raise RuntimeError(f"提交注册密码被 Cloudflare challenge 拦截，{_response_debug_detail(resp)}")
            data = _response_json(resp) if resp is not None else {}
            error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
            if _is_domain_rejected_error(data):
                domain = _email_domain(self.current_email)
                if domain:
                    mail_provider.disable_domain(domain)
                    step(index, f"注册失败提示: 邮箱域名 {domain} 被业务拒绝，已临时隔离", "yellow")
                else:
                    step(index, "注册失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
                raise MailboxRejectedForRetry(f"user_register_mailbox_rejected: {json.dumps(data, ensure_ascii=False)}")
            if _is_username_exists_error(data):
                step(index, f"注册失败提示: 邮箱 {self.current_email} 已存在，换邮箱重试", "yellow")
                raise MailboxRejectedForRetry(f"user_register_username_exists: {json.dumps(data, ensure_ascii=False)}")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            debug = _response_debug_detail(resp)
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}, {debug}")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp = None
        error = ""
        for attempt in range(1, 4):
            _throttle_proxy_authorize(self.session)
            resp, error = request_with_local_retry(
                self.session,
                "get",
                f"{auth_base}/api/accounts/email-otp/send",
                headers=self._navigate_headers(f"{auth_base}/create-account/password"),
                allow_redirects=True,
                verify=False,
                retry_attempts=1,
            )
            if resp is not None and resp.status_code in (200, 302):
                step(index, "发送验证码完成")
                return
            if resp is None and self._retry_after_network_error(error, index, "发送验证码", attempt):
                continue
            if not _is_cloudflare_challenge(resp) or attempt >= 3:
                break
            _mark_proxy_cloudflare(self.session, index, "发送验证码")
            step(index, f"发送验证码触发 Cloudflare challenge，重新预热后重试 {attempt}/3", "yellow")
            time.sleep(2 * attempt + random.uniform(0.5, 1.5))
            self._prewarm_platform(index)
        raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")

    def _validate_otp(self, code: str, index: int) -> None:
        step(index, f"开始校验验证码 {code}")
        resp = None
        error = ""
        for attempt in range(1, 4):
            _throttle_proxy_authorize(self.session)
            resp, error = validate_otp(self.session, self.device_id, code)
            if resp is not None and resp.status_code == 200:
                step(index, "验证码校验完成")
                return
            if resp is None and self._retry_after_network_error(error, index, "校验验证码", attempt):
                continue
            if not _is_cloudflare_challenge(resp) or attempt >= 3:
                break
            _mark_proxy_cloudflare(self.session, index, "校验验证码")
            step(index, f"校验验证码触发 Cloudflare challenge，重新预热后重试 {attempt}/3", "yellow")
            time.sleep(2 * attempt + random.uniform(0.5, 1.5))
            self._prewarm_platform(index)
        raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")

    def _create_account(self, name: str, birthdate: str, index: int) -> None:
        step(index, "开始创建账号资料")
        resp = None
        error = ""
        for attempt in range(1, 4):
            headers = self._json_headers(f"{auth_base}/about-you")
            headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
            _throttle_proxy_authorize(self.session)
            resp, error = request_with_local_retry(
                self.session,
                "post",
                f"{auth_base}/api/accounts/create_account",
                json={"name": name, "birthdate": birthdate},
                headers=headers,
                verify=False,
                retry_attempts=1,
            )
            if resp is not None and resp.status_code in (200, 302):
                data = _response_json(resp)
                callback_params = extract_oauth_callback_params_from_url(str(data.get("continue_url") or "").strip())
                self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
                step(index, "创建账号资料完成")
                return
            if resp is None and self._retry_after_network_error(error, index, "创建账号资料", attempt):
                continue
            if not _is_cloudflare_challenge(resp) or attempt >= 3:
                break
            _mark_proxy_cloudflare(self.session, index, "创建账号资料")
            step(index, f"创建账号资料触发 Cloudflare challenge，重新预热后重试 {attempt}/3", "yellow")
            time.sleep(2 * attempt + random.uniform(0.5, 1.5))
            self._prewarm_platform(index)

        data = _response_json(resp) if resp is not None else {}
        error_data = data.get("error") if isinstance(data.get("error"), dict) else {}
        if _is_domain_rejected_error(data):
            domain = _email_domain(self.current_email)
            if domain:
                mail_provider.disable_domain(domain)
                step(index, f"创建账号失败提示: 邮箱域名 {domain} 被业务拒绝，已临时隔离", "yellow")
            else:
                step(index, "创建账号失败提示: 邮箱域名很可能因滥用被封禁，请更换邮箱域名", "yellow")
            raise MailboxRejectedForRetry(f"create_account_mailbox_rejected: {json.dumps(data, ensure_ascii=False)}")
        if _is_username_exists_error(data):
            step(index, f"创建账号失败提示: 邮箱 {self.current_email} 已存在，换邮箱重试", "yellow")
            raise MailboxRejectedForRetry(f"create_account_duplicate_email: {json.dumps(data, ensure_ascii=False)}")
        detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
        raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}, {_response_debug_detail(resp)}")

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = None
        last_error = ""
        for attempt in range(1, 4):
            try:
                tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier)
                break
            except Exception as error:
                last_error = str(error)
                if not self._retry_after_network_error(last_error, index, "换 token", attempt):
                    break
        if not tokens:
            raise RuntimeError(last_error or "token换取失败")
        step(index, "token 换取完成")
        return tokens

    def _register_once(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        self.current_email = email
        label = str(mailbox.get("label") or "")
        step(index, f"邮箱创建完成[{label}]: {email}")
        password = _random_password()
        first_name, last_name = _random_name()
        with _serialized_proxy_flow(self.session, index):
            self._platform_authorize(email, index)
            self._register_user(email, password, index)
            self._send_otp(index)
        step(index, "开始等待注册验证码")
        code = wait_for_code(mailbox)
        if not code:
            raise MailCodeTimeoutForRetry(f"等待注册验证码超时: {email}")
        step(index, f"收到注册验证码: {code}")
        with _serialized_proxy_flow(self.session, index):
            self._validate_otp(code, index)
            self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            tokens = self._exchange_registered_tokens(index)
            _mark_proxy_success(self.session)
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": "web",
            "proxy": str(getattr(self.session, "_selected_proxy", "") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def register(self, index: int) -> dict:
        last_error = ""
        for mailbox_attempt in range(1, 4):
            try:
                if mailbox_attempt > 1:
                    step(index, f"重置会话并更换邮箱重试 {mailbox_attempt}/3", "yellow")
                    self._reset_flow_session()
                return self._register_once(index)
            except (MailboxRejectedForRetry, MailCodeTimeoutForRetry) as error:
                last_error = str(error)
                if mailbox_attempt >= 3:
                    raise RuntimeError(last_error) from error
                step(index, f"{last_error}，换邮箱重试", "yellow")
                time.sleep(1.0 + random.uniform(0.3, 1.2))
        raise RuntimeError(last_error or "mailbox_retry_failed")


_REGISTER_QUOTA_RESULT_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "email",
    "user_id",
    "type",
    "status",
    "quota",
    "image_quota_unknown",
    "limits_progress",
    "default_model_slug",
    "restore_at",
    "source_type",
    "proxy",
)


def _mark_registered_quota_unknown(access_token: str) -> None:
    account_service.update_account(
        access_token,
        {"status": "正常", "quota": 0, "image_quota_unknown": True},
        quiet=True,
    )


def _detect_registered_account_quota(index: int, access_token: str, result: dict) -> dict:
    """注册完成后立即检测一次远端 image2 额度，并写回账号池。"""
    try:
        step(index, "注册成功，开始检测 image2 额度")
        account = account_service.fetch_remote_info(access_token, "register_post_quota_check")
    except Exception as error:
        _mark_registered_quota_unknown(access_token)
        step(index, f"image2 额度检测失败，已保留未知额度: {str(error)[:200]}", "yellow")
        return result

    if not account:
        _mark_registered_quota_unknown(access_token)
        step(index, "image2 额度检测未返回结果，已保留未知额度", "yellow")
        return result

    quota = max(0, int(account.get("quota") or 0))
    image_quota_unknown = bool(account.get("image_quota_unknown"))
    if image_quota_unknown:
        step(index, "image2 额度检测完成：未知", "yellow")
    else:
        step(index, f"image2 额度检测完成：{quota}", "green" if quota > 0 else "yellow")

    for key in _REGISTER_QUOTA_RESULT_KEYS:
        if key in account and account.get(key) is not None:
            result[key] = account.get(key)
    return result


def worker(index: int) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        result.setdefault("status", "正常")
        result.setdefault("quota", 0)
        result.setdefault("image_quota_unknown", True)
        account_service.add_account_items([result])
        result = _detect_registered_account_quota(index, access_token, result)
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
