import json

import pytest

from clipengine.publishing import tokens


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
