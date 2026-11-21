"""Investment imports, holdings, broker cash flows, mutual fund orders."""
from datetime import date
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from app import portfolio
from app.analytics import to_jsonable
from app.auth import current_user
from app.db import get_session
from app.investment_import import summarize
from app.models import InvestmentImport

router = APIRouter(prefix='/api/investments', tags=['investments'])


@router.get('/imports')
def imports(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    rows = session.scalars(select(InvestmentImport).options(joinedload(InvestmentImport.account))
                           .where(InvestmentImport.user_id == user_id)
                           .order_by(InvestmentImport.created_at.desc())).all()
    return to_jsonable([summarize(i).model_dump() for i in rows])


@router.get('/holdings')
def holdings(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(portfolio.current_holdings(session, user_id))


@router.get('/holdings/history')
def history(isin: str | None = Query(None, pattern=r'^IN[A-Z0-9]{9}\d$'),
            session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(portfolio.holding_history(session, user_id, isin))


@router.get('/cash-flows')
def cash_flows(platform: str | None = Query(None, pattern=r'^(INDMONEY|GROWW|ZERODHA|UNKNOWN)$'),
               start: date | None = None, end: date | None = None,
               session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(portfolio.broker_cash_flows(session, user_id, platform, start, end))


@router.get('/fund-orders')
def fund_orders(start: date | None = None, end: date | None = None,
                session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return to_jsonable(portfolio.fund_orders(session, user_id, start, end))
