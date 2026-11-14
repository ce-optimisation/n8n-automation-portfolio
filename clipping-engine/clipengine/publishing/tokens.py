"""Secure local storage for OAuth tokens.

Preferred: the OS credential store via `keyring` (Windows Credential Manager,
macOS Keychain, Secret Service on Linux). Fallback: a JSON file in
data/secrets with owner-only permissions, used only when no OS store exists
(reported in the dashboard so the owner knows which is in use).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE = "local-clipping-engine"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "fail" in type(kr).__module__ or "null" in type(kr).__module__:
            return None
        return keyring
    except Exception:  # noqa: BLE001
        return None


class TokenStore:
    def __init__(self, secrets_dir: Path, use_keyring: bool = True):
        self.secrets_dir = Path(secrets_dir)
        self.kr = _keyring() if use_keyring else None

    @property
    def backend(self) -> str:
        return "OS credential store (keyring)" if self.kr else f"local file ({self.secrets_dir / 'tokens.json'})"

    def _file(self) -> Path:
        return self.secrets_dir / "tokens.json"

    def _read_file(self) -> dict:
        f = self._file()
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}

    def _write_file(self, data: dict) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        f = self._file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(f)

    def get(self, platform: str) -> dict | None:
        if self.kr:
            v = self.kr.get_password(SERVICE, platform)
            return json.loads(v) if v else None
        return self._read_file().get(platform)

    def set(self, platform: str, value: dict) -> None:
        if self.kr:
            self.kr.set_password(SERVICE, platform, json.dumps(value))
            return
        data = self._read_file()
        data[platform] = value
        self._write_file(data)

    def delete(self, platform: str) -> None:
        if self.kr:
            try:
                self.kr.delete_password(SERVICE, platform)
            except Exception:  # noqa: BLE001
                pass
            return
        data = self._read_file()
        data.pop(platform, None)
        self._write_file(data)
