"""Encrypted, atomic Canvas-cookie snapshots. One service process owns writes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SnapshotError(RuntimeError):
    pass


class SnapshotStore:
    def __init__(self, registry, key: bytes):
        self.registry = registry
        self._cipher = Fernet(key)

    def _path(self, user_id: str) -> Path:
        # Check before user_dir(): that existing helper creates directories.
        if not self.registry.is_approved(user_id):
            raise PermissionError("User is not approved")
        return self.registry.user_dir(user_id) / "canvas-session.enc"

    def load(self, user_id: str, origin: str) -> dict | None:
        path = self._path(user_id)
        try:
            encrypted = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            data = json.loads(self._cipher.decrypt(encrypted))
            if (not isinstance(data, dict) or data.get("schema") != 1
                    or data.get("user_id") != user_id or data.get("origin") != origin
                    or not isinstance(data.get("canvas_id"), str) or not data["canvas_id"]
                    or not isinstance(data.get("cookies"), list)
                    or type(data.get("version")) is not int or data["version"] < 1):
                raise ValueError("Invalid snapshot envelope")
            return data
        except (InvalidToken, ValueError, TypeError, KeyError):
            raise SnapshotError("Snapshot cannot be authenticated or belongs to another identity") from None

    def save(self, user_id: str, origin: str, canvas_id: str, cookies: list[dict]) -> None:
        old = self.load(user_id, origin)
        if old and old["canvas_id"] != canvas_id:
            raise SnapshotError("Refusing to replace the Canvas identity binding")
        data = {"schema": 1, "user_id": user_id, "origin": origin,
                "canvas_id": canvas_id, "version": old["version"] + 1 if old else 1,
                "cookies": cookies}
        encrypted = self._cipher.encrypt(json.dumps(data).encode("utf-8"))
        path = self._path(user_id)
        fd, name = tempfile.mkstemp(prefix=".canvas-session-", suffix=".enc", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encrypted)
                out.flush()
                os.fsync(out.fileno())
            # Recheck approval immediately before publication.
            self._path(user_id)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
