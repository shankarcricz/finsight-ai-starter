"""Bank statement imports, transactions, and analytics endpoints (single demo user for now)."""
from datetime import date
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import analytics
from app.auth import current_user
from app.db import get_session
from app.models import BankTransactionRecord, MerchantCategoryOverride, StatementImport
from app.schemas import CATEGORIES, Category
from app.statement_import import summarize

router = APIRouter(prefix='/api', tags=['bank'])


def _range(start: date | None, end: date | None):
    if start and end and start > end:
        raise HTTPException(400, 'start must be on or before end')
    return start, end


@router.get('/statements')
def list_statements(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    imports = session.scalars(select(StatementImport).where(StatementImport.user_id == user_id)
                              .order_by(StatementImport.created_at.desc())).all()
    return analytics.to_jsonable([summarize(i).model_dump() for i in imports])


@router.get('/analytics/summary')
def summary(start: date | None = None, end: date | None = None,
            session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return analytics.to_jsonable(analytics.income_expense_summary(session, user_id, *_range(start, end)))


@router.get('/analytics/monthly')
def monthly(start: date | None = None, end: date | None = None,
            session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return analytics.to_jsonable(analytics.monthly_cash_flow(session, user_id, *_range(start, end)))


@router.get('/analytics/categories')
def categories(start: date | None = None, end: date | None = None,
               session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return analytics.to_jsonable(analytics.category_breakdown(session, user_id, *_range(start, end)))


@router.get('/analytics/recurring')
def recurring(session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return analytics.to_jsonable(analytics.recurring_payments(session, user_id))


@router.get('/analytics/trends')
def trends(month: str | None = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
           session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    return analytics.to_jsonable(analytics.spending_trends(session, user_id, month))


@router.get('/transactions')
def transactions(start: date | None = None, end: date | None = None, category: Category | None = None,
                 type: Literal['debit', 'credit'] | None = None, search: str | None = Query(None, max_length=60),
                 needs_review: bool | None = None, limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0),
                 session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    start, end = _range(start, end)
    return analytics.to_jsonable(analytics.list_transactions(
        session, user_id, start=start, end=end, category=category, transaction_type=type, search=search,
        needs_review=needs_review, limit=limit, offset=offset))


class CategoryUpdate(BaseModel):
    category: Category
    apply_to_merchant: bool = False  # also remember merchant -> category for future imports


@router.patch('/transactions/{transaction_id}')
def update_category(transaction_id: str, body: CategoryUpdate,
                    session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    txn = session.scalar(select(BankTransactionRecord).where(
        BankTransactionRecord.id == transaction_id, BankTransactionRecord.user_id == user_id))
    if txn is None:
        raise HTTPException(404, 'Transaction not found')
    targets = [txn]
    if body.apply_to_merchant:
        override = session.scalar(select(MerchantCategoryOverride).where(
            MerchantCategoryOverride.user_id == user_id, MerchantCategoryOverride.merchant == txn.merchant))
        if override:
            override.category = body.category
        else:
            session.add(MerchantCategoryOverride(user_id=user_id, merchant=txn.merchant, category=body.category))
        # Re-label same-merchant rows that were not already corrected by hand.
        targets = session.scalars(select(BankTransactionRecord).where(
            BankTransactionRecord.user_id == user_id, BankTransactionRecord.merchant == txn.merchant,
            (BankTransactionRecord.category_source != 'manual') | (BankTransactionRecord.id == txn.id))).all()
    for t in targets:
        t.category, t.category_source, t.category_confidence = body.category, 'manual', 1.0
    return {'updated': len(targets), 'category': body.category, 'merchant': txn.merchant}


@router.get('/categories')
def list_categories():
    return list(CATEGORIES)
