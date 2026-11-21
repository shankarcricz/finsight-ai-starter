from collections import defaultdict
from pydantic import BaseModel, Field
from sqlalchemy import select
from app.db import session_scope
from app.models import Holding

class HoldingIn(BaseModel):
    broker: str = Field(min_length=1, max_length=60)
    symbol: str = Field(min_length=1, max_length=40)
    quantity: float = Field(gt=0)
    avg_cost: float = Field(ge=0)
    currency: str = 'INR'


def upsert_holding(user_id: str, h: HoldingIn):
    # IMPORTANT: This is user-confirmed demo data; NOT an automatic PDF extraction.
    # (Phase 3 replaces these float columns with Decimal, ISIN-based investment models.)
    with session_scope() as session:
        session.merge(Holding(user_id=user_id, broker=h.broker, symbol=h.symbol.upper(),
                              quantity=h.quantity, avg_cost=h.avg_cost, currency=h.currency.upper()))


def portfolio(user_id: str):
    with session_scope() as session:
        rows = [{'broker': h.broker, 'symbol': h.symbol, 'quantity': h.quantity, 'avg_cost': h.avg_cost,
                 'currency': h.currency}
                for h in session.scalars(select(Holding).where(Holding.user_id == user_id)
                                         .order_by(Holding.broker, Holding.symbol))]
    for row in rows:
        row['cost_basis'] = round(row['quantity'] * row['avg_cost'], 2)
    by_currency = defaultdict(float)
    for row in rows:
        by_currency[row['currency']] += row['cost_basis']
    return {'holdings': rows, 'cost_basis_by_currency': dict(by_currency),
            'note': 'Cost basis only. P&L and returns require verified market prices and corporate actions.'}
