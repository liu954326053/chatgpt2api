from __future__ import annotations

from services.register.mail_provider import VmailProvider, create_mailbox, wait_for_code


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.requests = []

    def request(self, method, url, json=None, timeout=None, verify=None):
        self.requests.append({"method": method, "url": url, "json": json, "timeout": timeout, "verify": verify})
        if url.endswith("/config"):
            return FakeResponse(200, {"emailDomain": ["example.test", "blocked.test", "example.test"]})
        if url.endswith("/api/emails"):
            return FakeResponse(200, {
                "emails": [
                    {
                        "id": "msg-1",
                        "to": json["address"],
                        "subject": "OpenAI verification code",
                        "text": "Your code is 123456",
                        "createdAt": "2026-06-11T00:00:00Z",
                    }
                ]
            })
        if url.endswith("/api/emails/msg-1"):
            return FakeResponse(200, {
                "id": "msg-1",
                "to": "user@example.test",
                "subject": "OpenAI verification code",
                "text": "Verification code: 123456",
                "createdAt": "2026-06-11T00:00:00Z",
            })
        return FakeResponse(404, {})

    def close(self):
        pass


def test_vmail_loads_domains_creates_mailbox_and_reads_code(monkeypatch):
    fake_session = FakeSession()
    monkeypatch.setattr("services.register.mail_provider._create_session", lambda conf: fake_session)

    mail_config = {
        "request_timeout": 5,
        "wait_timeout": 1,
        "wait_interval": 0.01,
        "providers": [{
            "enable": True,
            "type": "vmail",
            "api_base": "https://vmail.example.test",
            "domain": [],
            "auto_load_domains": True,
            "exclude_domains": ["blocked.test"],
        }],
    }

    mailbox = create_mailbox(mail_config, username="user")
    assert mailbox["provider"] == "vmail"
    assert mailbox["address"] == "user@example.test"

    code = wait_for_code(mail_config, mailbox)
    assert code == "123456"
    assert any(item["url"].endswith("/config") for item in fake_session.requests)
    assert any(item["url"].endswith("/api/emails") and item["json"] == {"address": "user@example.test"} for item in fake_session.requests)


def test_vmail_accepts_string_domain_and_boolean_config(monkeypatch):
    fake_session = FakeSession()
    monkeypatch.setattr("services.register.mail_provider._create_session", lambda conf: fake_session)

    provider = VmailProvider(
        {
            "type": "vmail",
            "api_base": "https://vmail.example.test",
            "domain": "one.test\ntwo.test",
            "auto_load_domains": "false",
            "exclude_domains": "two.test",
        },
        {"request_timeout": 5, "wait_timeout": 1, "wait_interval": 0.01, "user_agent": "pytest", "proxy": ""},
    )
    try:
        mailbox = provider.create_mailbox("user")
        assert mailbox["address"] == "user@one.test"
        assert not any(item["url"].endswith("/config") for item in fake_session.requests)
    finally:
        provider.close()
