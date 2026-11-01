"""Shared API keys, and the order in which each user's keys are tried.

A user can share one of their own API keys with another user without revealing
it: the recipient's requests are made with the owner's current key, and the
owner can revoke the share at any time. A bot admin can also share their OpenAI
key with everyone who may use Codex (the admin share).

For each service, a user's **credential chain** is every key they may use, in
their chosen order: their own key, keys shared with them, and the admin share.
A request starts on the first and moves down the chain when a key fails on
authentication, credit or rate limits. See docs/key_sharing.md.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, List, Optional, Tuple

from sqlalchemy import Column, DateTime, Integer, String

from uniborg import llm_db

#: Kinds of credential source.
OWN = "own"
SHARE = "share"
ADMIN = "admin"

#: Services the admin share covers.
ADMIN_SHARE_SERVICES = ("openai",)

#: Per-user override of the admin share.
FOLLOW = "follow"
ON = "on"
OFF = "off"
OVERRIDE_MODES = (FOLLOW, ON, OFF)


class KeyShareRecord(llm_db.Base):
    __tablename__ = "key_shares"
    owner_id = Column(Integer, primary_key=True, autoincrement=False)
    recipient_id = Column(Integer, primary_key=True, autoincrement=False)
    service = Column(String, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class KeyOrderRecord(llm_db.Base):
    __tablename__ = "key_orders"
    user_id = Column(Integer, primary_key=True, autoincrement=False)
    service = Column(String, primary_key=True)
    #: JSON: {"order": [token, ...], "disabled": [token, ...]}.
    data = Column(String, nullable=False)


class AdminSharePolicyRecord(llm_db.Base):
    __tablename__ = "admin_share_policies"
    service = Column(String, primary_key=True)
    owner_id = Column(Integer, nullable=False)
    enabled = Column(Integer, nullable=False)


class AdminShareOverrideRecord(llm_db.Base):
    __tablename__ = "admin_share_overrides"
    service = Column(String, primary_key=True)
    user_id = Column(Integer, primary_key=True, autoincrement=False)
    mode = Column(String, nullable=False)


llm_db.Base.metadata.create_all(llm_db.engine)


def _session():
    return llm_db.Session()


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def token_for(kind: str, owner_id: Optional[int] = None) -> str:
    """The stable name of a source in a saved order: `own`, `share:42`, `admin:7`."""
    if kind == OWN:
        return OWN
    if kind in (SHARE, ADMIN):
        return f"{kind}:{owner_id}"
    raise ValueError(f"Unknown credential source kind: {kind!r}")


# --- Shares ---


@dataclass(frozen=True)
class KeyShare:
    owner_id: int
    recipient_id: int
    service: str
    created_at: Optional[datetime]


def _share(row) -> KeyShare:
    return KeyShare(row.owner_id, row.recipient_id, row.service, _utc(row.created_at))


def share_key(*, owner_id: int, recipient_id: int, service: str) -> bool:
    """Share OWNER_ID's SERVICE key with RECIPIENT_ID. False if already shared."""
    if owner_id == recipient_id:
        raise ValueError("A key cannot be shared with its owner.")
    session = _session()
    try:
        exists = session.get(KeyShareRecord, (owner_id, recipient_id, service))
        if exists is not None:
            return False
        session.add(
            KeyShareRecord(
                owner_id=owner_id,
                recipient_id=recipient_id,
                service=service,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        return True
    except:
        session.rollback()
        raise
    finally:
        session.close()


def revoke_share(*, owner_id: int, recipient_id: int, service: str) -> bool:
    session = _session()
    try:
        row = session.get(KeyShareRecord, (owner_id, recipient_id, service))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    except:
        session.rollback()
        raise
    finally:
        session.close()


def shares_by(owner_id: int) -> List[KeyShare]:
    session = _session()
    try:
        rows = (
            session.query(KeyShareRecord)
            .filter(KeyShareRecord.owner_id == owner_id)
            .order_by(KeyShareRecord.service, KeyShareRecord.created_at)
            .all()
        )
        return [_share(row) for row in rows]
    finally:
        session.close()


def shares_to(recipient_id: int, service: Optional[str] = None) -> List[KeyShare]:
    session = _session()
    try:
        query = session.query(KeyShareRecord).filter(
            KeyShareRecord.recipient_id == recipient_id
        )
        if service is not None:
            query = query.filter(KeyShareRecord.service == service)
        rows = query.order_by(KeyShareRecord.created_at).all()
        return [_share(row) for row in rows]
    finally:
        session.close()


# --- Admin share ---


@dataclass(frozen=True)
class AdminSharePolicy:
    service: str
    owner_id: int
    enabled: bool


def admin_share_policy(service: str) -> Optional[AdminSharePolicy]:
    session = _session()
    try:
        row = session.get(AdminSharePolicyRecord, service)
        if row is None:
            return None
        return AdminSharePolicy(row.service, row.owner_id, bool(row.enabled))
    finally:
        session.close()


def set_admin_share_policy(*, service: str, owner_id: int, enabled: bool) -> None:
    if service not in ADMIN_SHARE_SERVICES:
        raise ValueError(f"The admin share does not cover {service!r}")
    session = _session()
    try:
        row = session.get(AdminSharePolicyRecord, service)
        if row is None:
            session.add(
                AdminSharePolicyRecord(
                    service=service, owner_id=owner_id, enabled=int(enabled)
                )
            )
        else:
            row.owner_id = owner_id
            row.enabled = int(enabled)
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def admin_share_override(*, service: str, user_id: int) -> str:
    session = _session()
    try:
        row = session.get(AdminShareOverrideRecord, (service, user_id))
        return row.mode if row is not None and row.mode in OVERRIDE_MODES else FOLLOW
    finally:
        session.close()


def set_admin_share_override(*, service: str, user_id: int, mode: str) -> None:
    if mode not in OVERRIDE_MODES:
        raise ValueError(f"Unknown admin share override: {mode!r}")
    session = _session()
    try:
        row = session.get(AdminShareOverrideRecord, (service, user_id))
        if mode == FOLLOW:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(
                AdminShareOverrideRecord(service=service, user_id=user_id, mode=mode)
            )
        else:
            row.mode = mode
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def admin_share_applies(*, service: str, user_id: int, codex_access: bool) -> bool:
    """Whether the admin share of SERVICE reaches USER_ID on this request."""
    policy = admin_share_policy(service)
    if policy is None or not policy.enabled or policy.owner_id == user_id:
        return False
    mode = admin_share_override(service=service, user_id=user_id)
    if mode == ON:
        return True
    if mode == OFF:
        return False
    return codex_access


# --- Order ---


@dataclass(frozen=True)
class KeyOrder:
    order: Tuple[str, ...] = ()
    disabled: Tuple[str, ...] = ()


def key_order(*, user_id: int, service: str) -> KeyOrder:
    session = _session()
    try:
        row = session.get(KeyOrderRecord, (user_id, service))
    finally:
        session.close()
    if row is None:
        return KeyOrder()
    try:
        data = json.loads(row.data)
        return KeyOrder(
            tuple(str(t) for t in data.get("order", [])),
            tuple(str(t) for t in data.get("disabled", [])),
        )
    except (ValueError, AttributeError):
        return KeyOrder()


def _save_key_order(*, user_id: int, service: str, order: KeyOrder) -> None:
    data = json.dumps({"order": list(order.order), "disabled": list(order.disabled)})
    session = _session()
    try:
        row = session.get(KeyOrderRecord, (user_id, service))
        if row is None:
            session.add(KeyOrderRecord(user_id=user_id, service=service, data=data))
        else:
            row.data = data
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


# --- Sources and chains ---


@dataclass(frozen=True)
class Source:
    """One place a user's SERVICE key can come from."""

    token: str
    kind: str
    owner_id: int
    #: The owner has a key for the service right now.
    available: bool
    enabled: bool = True


@dataclass(frozen=True)
class Credential:
    """A key a request can use, and whose it is."""

    api_key: str = field(repr=False)
    token: str
    kind: str
    owner_id: int


OwnKey = Callable[[int, str], Optional[str]]


def _owner_key(owner_id: int, service: str) -> Optional[str]:
    return llm_db.get_api_key(owner_id, service=service)


def _default_sources(
    user_id: int, service: str, *, codex_access: bool, own_key: OwnKey
) -> List[Tuple[Source, Optional[str]]]:
    found = [
        (
            Source(OWN, OWN, user_id, available=False),
            own_key(user_id, service),
        )
    ]
    for share in shares_to(user_id, service):
        found.append(
            (
                Source(token_for(SHARE, share.owner_id), SHARE, share.owner_id, False),
                _owner_key(share.owner_id, service),
            )
        )
    if service in ADMIN_SHARE_SERVICES and admin_share_applies(
        service=service, user_id=user_id, codex_access=codex_access
    ):
        owner_id = admin_share_policy(service).owner_id
        token = token_for(ADMIN, owner_id)
        if all(source.owner_id != owner_id for source, _ in found):
            found.append(
                (Source(token, ADMIN, owner_id, False), _owner_key(owner_id, service))
            )
    return [
        (Source(s.token, s.kind, s.owner_id, available=bool(key)), key)
        for s, key in found
    ]


def _ordered(items, order: KeyOrder):
    rank = {token: index for index, token in enumerate(order.order)}
    #: `sorted` is stable, so sources missing from the saved order keep their
    #: default places after the saved ones.
    return sorted(items, key=lambda item: rank.get(item[0].token, len(rank)))


def sources(
    user_id: int, service: str, *, codex_access: bool, own_key: OwnKey = _owner_key
) -> List[Source]:
    """Every source of USER_ID's SERVICE key, in their order, for display."""
    order = key_order(user_id=user_id, service=service)
    items = _ordered(
        _default_sources(user_id, service, codex_access=codex_access, own_key=own_key),
        order,
    )
    return [
        Source(s.token, s.kind, s.owner_id, s.available, s.token not in order.disabled)
        for s, _ in items
    ]


def chain(
    user_id: int, service: str, *, codex_access: bool, own_key: OwnKey = _owner_key
) -> List[Credential]:
    """The keys USER_ID's SERVICE requests try, first to last.

    Disabled sources and owners without a key are left out, and so is a key
    that already appeared earlier (an owner who shares the key you hold).
    """
    order = key_order(user_id=user_id, service=service)
    items = _ordered(
        _default_sources(user_id, service, codex_access=codex_access, own_key=own_key),
        order,
    )
    result, seen = [], set()
    for source, key in items:
        if not key or source.token in order.disabled or key in seen:
            continue
        seen.add(key)
        result.append(Credential(key, source.token, source.kind, source.owner_id))
    return result


def move_source(
    *, user_id: int, service: str, token: str, delta: int, current: Iterable[str]
) -> None:
    """Move TOKEN by DELTA places within CURRENT, the order as displayed."""
    tokens = list(current)
    if token not in tokens:
        return
    index = tokens.index(token)
    target = max(0, min(len(tokens) - 1, index + delta))
    tokens.insert(target, tokens.pop(index))
    previous = key_order(user_id=user_id, service=service)
    _save_key_order(
        user_id=user_id,
        service=service,
        order=KeyOrder(tuple(tokens), previous.disabled),
    )


def toggle_source(*, user_id: int, service: str, token: str) -> bool:
    """Disable an enabled source or enable a disabled one; returns enabled."""
    previous = key_order(user_id=user_id, service=service)
    disabled = list(previous.disabled)
    if token in disabled:
        disabled.remove(token)
        enabled = True
    else:
        disabled.append(token)
        enabled = False
    _save_key_order(
        user_id=user_id,
        service=service,
        order=KeyOrder(previous.order, tuple(disabled)),
    )
    return enabled


# --- Failures ---


class KeyFailure(str, Enum):
    """Why a key could not answer, when trying the next key may help."""

    AUTH = "rejected"
    CREDIT = "out of credit"
    RATE_LIMIT = "rate limited"


def _causes(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        original = getattr(exc, "original_exception", None)
        exc = original or exc.__cause__ or exc.__context__


def classify_key_failure(exc: BaseException) -> Optional[KeyFailure]:
    """The KeyFailure EXC shows, or None when another key would not help."""
    found = None
    for item in _causes(exc):
        text = str(item)
        status = getattr(item, "status_code", None)
        name = type(item).__name__
        if "insufficient_quota" in text or status == 402:
            return KeyFailure.CREDIT
        if status in (401, 403) or name in (
            "AuthenticationError",
            "PermissionDeniedError",
        ):
            found = found or KeyFailure.AUTH
        elif status == 429 or name in ("RateLimitError", "RateLimitException"):
            found = found or KeyFailure.RATE_LIMIT
    return found
