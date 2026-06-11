from __future__ import annotations

import os
import time

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from services.register import openai_register


class FakeLease:
    def __init__(self, proxy: str) -> None:
        self.proxy = proxy
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeRegistrar:
    instances: list["FakeRegistrar"] = []

    def __init__(self, proxy: str) -> None:
        self.proxy = proxy
        self.closed = False
        self.proxy_lease = FakeLease("http://proxy.example.com:8080")
        FakeRegistrar.instances.append(self)

    def register(self, index: int) -> dict:
        return {
            "email": f"user{index}@example.com",
            "password": "pwd",
            "access_token": "token-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
            "source_type": "web",
            "proxy": "http://proxy.example.com:8080",
        }

    def close(self) -> None:
        self.closed = True
        if self.proxy_lease is not None:
            self.proxy_lease.release()
            self.proxy_lease = None


class FakeAccountService:
    def __init__(self, *, fetch_result: dict | None = None, fetch_error: Exception | None = None) -> None:
        self.fetch_result = fetch_result
        self.fetch_error = fetch_error
        self.added: list[list[dict]] = []
        self.fetched: list[tuple[str, str]] = []
        self.updated: list[tuple[str, dict, bool]] = []

    def add_account_items(self, items: list[dict]) -> None:
        self.added.append([dict(item) for item in items])

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict | None:
        self.fetched.append((access_token, event))
        if self.fetch_error is not None:
            raise self.fetch_error
        return dict(self.fetch_result) if self.fetch_result is not None else None

    def update_account(self, access_token: str, updates: dict, quiet: bool = False) -> dict | None:
        self.updated.append((access_token, dict(updates), quiet))
        return {"access_token": access_token, **updates}


def _prepare_worker(monkeypatch, account_service: FakeAccountService) -> None:
    FakeRegistrar.instances.clear()
    monkeypatch.setattr(openai_register, "PlatformRegistrar", FakeRegistrar)
    monkeypatch.setattr(openai_register, "account_service", account_service)
    monkeypatch.setattr(openai_register, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(openai_register, "step", lambda *args, **kwargs: None)
    openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})


def test_worker_detects_and_saves_quota_after_registration(monkeypatch) -> None:
    account_service = FakeAccountService(
        fetch_result={
            "access_token": "token-1",
            "email": "user1@example.com",
            "type": "Plus",
            "status": "正常",
            "quota": 42,
            "image_quota_unknown": False,
            "default_model_slug": "gpt-5-5",
            "restore_at": "2026-06-11T12:00:00Z",
        }
    )
    _prepare_worker(monkeypatch, account_service)

    response = openai_register.worker(1)

    assert response["ok"] is True
    assert account_service.added[0][0]["access_token"] == "token-1"
    assert account_service.fetched == [("token-1", "register_post_quota_check")]
    assert account_service.updated == []
    assert response["result"]["quota"] == 42
    assert response["result"]["image_quota_unknown"] is False
    assert response["result"]["type"] == "Plus"
    assert FakeRegistrar.instances[-1].closed is True
    assert FakeRegistrar.instances[-1].proxy_lease is None


def test_worker_keeps_registration_success_when_quota_check_fails(monkeypatch) -> None:
    account_service = FakeAccountService(fetch_error=RuntimeError("remote temporarily unavailable"))
    _prepare_worker(monkeypatch, account_service)

    response = openai_register.worker(2)

    assert response["ok"] is True
    assert account_service.fetched == [("token-1", "register_post_quota_check")]
    assert account_service.updated == [
        (
            "token-1",
            {"status": "正常", "quota": 0, "image_quota_unknown": True},
            True,
        )
    ]
    assert response["result"]["access_token"] == "token-1"
    assert FakeRegistrar.instances[-1].closed is True
