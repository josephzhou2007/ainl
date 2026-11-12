"""Composition point for the future scale-up dispatcher; no import-time I/O."""
from dataclasses import dataclass

from .sessions import PooledBrowser, SessionManager
from .store import SnapshotStore
from .tickets import LoginTickets


@dataclass(frozen=True)
class Services:
    sessions: SessionManager
    tickets: LoginTickets


def build_services(registry, existing_pool, verify_token, *, origin: str, encryption_key: bytes):
    """Inject the *same* Registry/BrowserPool used by the host dispatcher.

    verify_token is typically lambda value: tokens.verify(registry, value).
    The key comes from the host secret store, never the registry/profile folder.
    """
    return Services(
        SessionManager(registry, PooledBrowser(existing_pool),
                       SnapshotStore(registry, encryption_key), origin),
        LoginTickets(verify_token),
    )
