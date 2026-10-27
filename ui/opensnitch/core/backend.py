"""Immutable frontend boundary. Implementations deliver callbacks on the UI thread."""
from dataclasses import dataclass
from typing import Callable, Protocol, Tuple
from .identity import ApplicationIdentity
from .rules import RuleRecord


@dataclass(frozen=True)
class ApplicationSnapshot:
    identity: ApplicationIdentity
    sources: Tuple[str, ...]
    last_seen: float
    available: bool | None = None
    policy: str = 'Unavailable'
    pending: bool = False
    error: str = ''
    action: str = ''
    duration: str = ''
    related_rules: Tuple[str, ...] = ()
    global_rules: Tuple[str, ...] = ()
    other_rule_details: Tuple[Tuple[str, str], ...] = ()

    @property
    def key(self):
        return self.identity.node_id, self.identity.executable_path


@dataclass(frozen=True)
class BackendSnapshot:
    applications: Tuple[ApplicationSnapshot, ...]
    connected: bool = False
    authoritative: bool = False
    message: str = ''
    rules: Tuple[RuleRecord, ...] = ()


class ApplicationBackend(Protocol):
    def subscribe(self, callback: Callable[[BackendSnapshot], None]) -> Callable[[], None]: ...
    def request_policy(self, key: tuple[str, str], action: str, duration: str = 'always') -> bool: ...
    def close(self) -> None: ...
