"""Markets tab: FII/DII flows and NIFTY history (market-wide data, still behind sign-in)."""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session
from app import markets
from app.analytics import to_jsonable
from app.auth import admin_user, current_user
from app.db import get_session
from app.market_data import MarketDataError

router = APIRouter(prefix='/api/markets', tags=['markets'])


@router.get('/fii-dii')
def fii_dii(days: int = Query(90, ge=5, le=2000), refresh: bool = True,
            session: Session = Depends(get_session), user_id: str = Depends(current_user)):
    status = markets.refresh_fii_dii(session) if refresh else {'refreshed': False}
    return to_jsonable({**markets.flows(session, days), 'fetch': status})


@router.post('/fii-dii/import')
def fii_dii_import(file: UploadFile = File(...), session: Session = Depends(get_session),
                   user_id: str = Depends(admin_user)):
    # market_flows is shared by every user, so only admins may write to it.
    raw = file.file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(400, 'CSV exceeds 5 MB')
    try:
        return markets.import_flows_csv(session, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/index')
def index(symbol: str = Query('^NSEI', pattern=r'^\^(NSEI|NSEBANK|BSESN|CNXIT)$'),
          range: str = Query('1y', pattern=r'^(1mo|6mo|1y|5y)$'), user_id: str = Depends(current_user)):
    try:
        return to_jsonable(markets.index_history(symbol, range))
    except MarketDataError as exc:
        raise HTTPException(503, str(exc)) from exc
