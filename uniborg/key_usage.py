"""Approximate usage per day, user, payer and backend.

Each answered request adds to one daily row. The **payer** is whose access paid
for it: the owner of the API key the request used, or `CODEX_PAYER` for the
shared Codex subscription. Sharers and admins read these rows to see how much of
their access others use. See docs/key_sharing.md.

Token counts are known for Codex and the OpenAI API, whose responses report
them. Other services record request counts only. Costs are estimates from list
prices (`PRICES`), not bills.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import Column, Float, Integer, String

from uniborg import llm_db

#: The payer id of the shared Codex subscription.
CODEX_PAYER = 0

#: Backends usage is broken down by.
CODEX = "codex"
OPENAI = "openai"


@dataclass(frozen=True)
class Price:
    """US dollars per token."""

    input: float
    output: float
    #: Cached input, as a fraction of the input price.
    cached_fraction: float = 0.1


#: Approximate list prices by GPT family, matched by name in the model id
#: (OpenRouter's catalog, which mirrors OpenAI's, 2026-11). Codex requests are
#: priced as if they had run on the API, to show what the subscription saves.
PRICES: Dict[str, Price] = {
    "sol": Price(2e-6, 1e-5),
    "astra": Price(1e-5, 5e-5),
    "luna": Price(1e-7, 5e-7),
}


def price_for(model: str) -> Optional[Price]:
    name = model.rsplit("/", 1)[-1].lower()
    for family, price in PRICES.items():
        if family in name:
            return price
    return None


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    cached_input: int = 0
    output: int = 0


def estimate_cost(model: str, tokens: Optional[Tokens]) -> Optional[float]:
    price = price_for(model)
    if price is None or tokens is None:
        return None
    fresh = max(tokens.input - tokens.cached_input, 0)
    return (
        fresh * price.input
        + tokens.cached_input * price.input * price.cached_fraction
        + tokens.output * price.output
    )


class KeyUsageRecord(llm_db.Base):
    __tablename__ = "key_usage_daily"
    day = Column(String, primary_key=True)
    payer_id = Column(Integer, primary_key=True, autoincrement=False)
    user_id = Column(Integer, primary_key=True, autoincrement=False)
    backend = Column(String, primary_key=True)
    model = Column(String, primary_key=True)
    requests = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=False, default=0)
    cached_input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    #: Requests whose response reported token counts.
    metered_requests = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)


llm_db.Base.metadata.create_all(llm_db.engine)

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(
    *,
    payer_id: int,
    user_id: int,
    backend: str,
    model: str,
    tokens: Optional[Tokens] = None,
    clock: Clock = _now,
) -> None:
    """Add one answered request to today's row."""
    day = clock().strftime("%Y-%m-%d")
    key = (day, payer_id, user_id, backend, model)
    cost = estimate_cost(model, tokens) or 0.0
    session = llm_db.Session()
    try:
        row = session.get(KeyUsageRecord, key)
        if row is None:
            row = KeyUsageRecord(
                day=day,
                payer_id=payer_id,
                user_id=user_id,
                backend=backend,
                model=model,
                requests=0,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                metered_requests=0,
                cost_usd=0.0,
            )
            session.add(row)
        row.requests += 1
        if tokens is not None:
            row.metered_requests += 1
            row.input_tokens += tokens.input
            row.cached_input_tokens += tokens.cached_input
            row.output_tokens += tokens.output
        row.cost_usd += cost
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


@dataclass
class UsageTotal:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: Some requests reported no tokens, so the cost is a lower bound.
    partial: bool = False

    def add(self, row: KeyUsageRecord) -> None:
        self.requests += row.requests
        self.input_tokens += row.input_tokens
        self.output_tokens += row.output_tokens
        self.cost_usd += row.cost_usd
        if row.metered_requests < row.requests:
            self.partial = True


#: How far back the panels look.
DEFAULT_DAYS = 30


def _rows(
    *,
    payer_id: Optional[int] = None,
    user_id: Optional[int] = None,
    days: int = DEFAULT_DAYS,
    clock: Clock = _now,
) -> List[KeyUsageRecord]:
    since = (clock() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    session = llm_db.Session()
    try:
        query = session.query(KeyUsageRecord).filter(KeyUsageRecord.day >= since)
        if payer_id is not None:
            query = query.filter(KeyUsageRecord.payer_id == payer_id)
        if user_id is not None:
            query = query.filter(KeyUsageRecord.user_id == user_id)
        return query.all()
    finally:
        session.close()


def by_user(
    *, payer_id: int, days: int = DEFAULT_DAYS, clock: Clock = _now
) -> Dict[Tuple[int, str], UsageTotal]:
    """Others' use of PAYER_ID's access: {(user_id, backend): total}."""
    totals: Dict[Tuple[int, str], UsageTotal] = {}
    for row in _rows(payer_id=payer_id, days=days, clock=clock):
        if row.user_id == payer_id:
            continue
        totals.setdefault((row.user_id, row.backend), UsageTotal()).add(row)
    return totals


def of_user(
    user_id: int, *, days: int = DEFAULT_DAYS, clock: Clock = _now
) -> Dict[Tuple[int, str], UsageTotal]:
    """USER_ID's usage: {(payer_id, backend): total}."""
    totals: Dict[Tuple[int, str], UsageTotal] = {}
    for row in _rows(user_id=user_id, days=days, clock=clock):
        totals.setdefault((row.payer_id, row.backend), UsageTotal()).add(row)
    return totals


def describe(total: UsageTotal) -> str:
    """`12 requests, 340k tokens, ~$0.42`, for a panel line."""
    parts = [f"{total.requests} request{'s' if total.requests != 1 else ''}"]
    tokens = total.input_tokens + total.output_tokens
    if tokens:
        parts.append(f"{_compact(tokens)} tokens")
    if total.cost_usd:
        parts.append(f"~${total.cost_usd:.2f}" + ("+" if total.partial else ""))
    return ", ".join(parts)


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)
