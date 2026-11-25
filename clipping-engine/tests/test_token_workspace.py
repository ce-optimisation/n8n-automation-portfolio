import json

import pytest

from clipengine.publishing import tokens
from clipengine.config import get_settings
from clipengine.web.app import create_app


class MemoryKeyring:
    def __init__(self):
        self.entries = {}

    def get_password(self, service, account):
        return self.entries.get((service, account))

    def set_password(self, service, account, value):
        self.entries[(service, account)] = value

    def delete_password(self, service, account):
        del self.entries[(service, account)]


@pytest.fixture(autouse=True)
def isolated_credential_store(monkeypatch):
    # This module exercises the keyring path without allowing real keyring access.
    keyring = MemoryKeyring()
    monkeypatch.setattr(tokens, "_keyring", lambda: keyring)
    return keyring


@pytest.mark.parametrize("use_keyring", [True, False])
def test_workspace_credentials_are_distinct_and_legacy_readable(tmp_path, isolated_credential_store, use_keyring):
    account = "youtube:example"
    legacy_value = {"access_token": "legacy-fixture"}
    if use_keyring:
        isolated_credential_store.set_password(tokens.SERVICE, account, json.dumps(legacy_value))
    else:
        (tmp_path / "tokens.json").write_text(json.dumps({account: legacy_value}), encoding="utf-8")

    legacy = tokens.TokenStore(tmp_path, use_keyring)
    first = tokens.TokenStore(tmp_path, use_keyring, workspace_id="workspace-one")
    second = tokens.TokenStore(tmp_path, use_keyring, workspace_id="workspace-two")
    assert legacy.get(account) == legacy_value
    assert first.get(account) is None
    assert second.get(account) is None

    first.set(account, {"access_token": "first-fixture"})
    second.set(account, {"access_token": "second-fixture"})
    assert first.get(account) == {"access_token": "first-fixture"}
    assert second.get(account) == {"access_token": "second-fixture"}
    first.delete(account)
    assert first.get(account) is None
    assert second.get(account) == {"access_token": "second-fixture"}
    assert legacy.get(account) == legacy_value


@pytest.mark.parametrize("workspace_id", ["", "../escape", "Mixed", "space here", "a" * 49, 42])
def test_workspace_credentials_reject_invalid_identifiers(tmp_path, workspace_id):
    with pytest.raises(ValueError, match="Workspace ID"):
        tokens.TokenStore(tmp_path, use_keyring=False, workspace_id=workspace_id)


def test_engine_and_cli_store_factory_use_the_configured_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPENGINE_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("CLIPENGINE_WORKSPACE_ID", raising=False)
    account = "youtube:example"
    legacy = get_settings(data_dir=tmp_path / "legacy")
    legacy_store = tokens.TokenStore.for_settings(legacy)
    legacy_store.set(account, {"access_token": "legacy"})
    assert legacy.workspace_id is None
    assert legacy_store.service == tokens.SERVICE

    monkeypatch.setenv("CLIPENGINE_WORKSPACE_ID", "customer-one")
    first = get_settings(data_dir=tmp_path / "first")
    first_store = create_app(first, start_worker=False).state.core.tokens
    assert first_store.get(account) is None
    first_store.set(account, {"access_token": "first"})

    monkeypatch.setenv("CLIPENGINE_WORKSPACE_ID", "customer-two")
    second = get_settings(data_dir=tmp_path / "second")
    second_store = create_app(second, start_worker=False).state.core.tokens
    assert second_store.get(account) is None
    second_store.set(account, {"access_token": "second"})

    for settings, expected in ((legacy, "legacy"), (first, "first"), (second, "second")):
        store = tokens.TokenStore.for_settings(settings)
        assert store.get(account) == {"access_token": expected}
