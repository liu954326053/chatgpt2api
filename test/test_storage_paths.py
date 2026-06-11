from __future__ import annotations

from pathlib import Path

from services.config import ConfigStore
from services.storage.factory import create_storage_backend
from services.storage.json_storage import JSONStorageBackend


def test_accounts_path_env_can_point_to_json_file(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"auth-key":"test-auth"}\n', encoding="utf-8")
    accounts_file = tmp_path / "persist" / "my-accounts.json"
    monkeypatch.setenv("CHATGPT2API_ACCOUNTS_PATH", str(accounts_file))

    store = ConfigStore(config_file)

    assert store.accounts_file == accounts_file


def test_accounts_path_env_can_point_to_directory(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text('{"auth-key":"test-auth"}\n', encoding="utf-8")
    accounts_dir = tmp_path / "persist"
    monkeypatch.setenv("CHATGPT2API_ACCOUNTS_PATH", str(accounts_dir))

    store = ConfigStore(config_file)

    assert store.accounts_file == accounts_dir / "accounts.json"


def test_json_storage_backend_uses_accounts_path_env(monkeypatch, tmp_path: Path) -> None:
    accounts_file = tmp_path / "persist" / "accounts.json"
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    monkeypatch.setenv("CHATGPT2API_ACCOUNTS_PATH", str(accounts_file))

    backend = create_storage_backend(tmp_path / "data")

    assert isinstance(backend, JSONStorageBackend)
    assert backend.file_path == accounts_file
    assert backend.auth_keys_path == accounts_file.with_name("auth_keys.json")
