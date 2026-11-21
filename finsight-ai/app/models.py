"""SQLAlchemy ORM models. SQL (not Pinecone) is the source of truth for financial records."""
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, func, true)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

CENT = Decimal('0.01')


class ScaledDecimal(TypeDecorator):
    """Exact decimal stored as a scaled integer (e.g. scale=2 -> paise), exposed as Decimal.

    SQLite has no exact decimal type (Numeric becomes a float), so we never let money touch floats.
    Values with more decimal places than `scale` are rejected rather than silently rounded.
    """
    impl = BigInteger
    cache_ok = True
    scale = 2

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(value)
        step = Decimal(1).scaleb(-self.scale)
        if value != value.quantize(step, rounding=ROUND_HALF_UP):
            raise ValueError(f'{type(self).__name__} value has more than {self.scale} decimal places: {value}')
        return int(value.scaleb(self.scale))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value).scaleb(-self.scale).quantize(Decimal(1).scaleb(-self.scale))


class Money(ScaledDecimal):
    scale = 2


class Quantity(ScaledDecimal):
    scale = 8  # fractional US shares (e.g. 0.93130636); MF units have 3 decimals


class Price(ScaledDecimal):
    scale = 6  # NAVs have 4 decimals; US average costs are reported to ~9 and rounded to 6


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = (UniqueConstraint('user_id', 'sha256', 'category'),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer)
    # False when uploaded "local only": never embedded or sent to Pinecone.
    indexed_in_rag: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[str] = mapped_column(Text, server_default=func.current_timestamp())


class Holding(Base):
    # Legacy Phase 1 table (user-confirmed holdings). Phase 3 replaces it with ISIN-based models.
    __tablename__ = 'holdings'
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    broker: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    quantity: Mapped[float] = mapped_column(Float)
    avg_cost: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(Text, default='INR')
    updated_at: Mapped[str] = mapped_column(Text, server_default=func.current_timestamp())


class Alert(Base):
    __tablename__ = 'alerts'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    threshold: Mapped[float] = mapped_column(Float)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_sent_date: Mapped[str | None] = mapped_column(Text)


class FinancialAccount(Base):
    __tablename__ = 'financial_accounts'
    __table_args__ = (UniqueConstraint('user_id', 'institution', 'account_mask'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    institution: Mapped[str] = mapped_column(String(80))
    # Only the last 4 characters of the account number are stored.
    account_mask: Mapped[str] = mapped_column(String(16))
    account_type: Mapped[str] = mapped_column(String(20), default='bank')
    currency: Mapped[str] = mapped_column(String(3), default='INR')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StatementImport(Base):
    """One structured extraction run of one document, including its reconciliation outcome."""
    __tablename__ = 'statement_imports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), unique=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey('financial_accounts.id'))
    parser: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24))  # imported | needs_review | unsupported_layout
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default='INR')
    opening_balance: Mapped[Decimal | None] = mapped_column(Money)
    closing_balance: Mapped[Decimal | None] = mapped_column(Money)
    stated_total_credits: Mapped[Decimal | None] = mapped_column(Money)
    stated_total_debits: Mapped[Decimal | None] = mapped_column(Money)
    reconciliation_status: Mapped[str] = mapped_column(String(20))  # reconciled | mismatch | unverifiable
    reconciliation_json: Mapped[str] = mapped_column(Text, default='{}')
    warnings_json: Mapped[str] = mapped_column(Text, default='[]')
    transactions_found: Mapped[int] = mapped_column(Integer, default=0)
    transactions_inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[FinancialAccount | None] = relationship()


class BankTransactionRecord(Base):
    __tablename__ = 'bank_transactions'
    __table_args__ = (UniqueConstraint('account_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('financial_accounts.id'), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    import_id: Mapped[str] = mapped_column(ForeignKey('statement_imports.id'))
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_row: Mapped[int | None] = mapped_column(Integer)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    original_description: Mapped[str] = mapped_column(String(500))
    merchant: Mapped[str] = mapped_column(String(120), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)  # always positive; direction is transaction_type
    transaction_type: Mapped[str] = mapped_column(String(6))  # credit | debit
    currency: Mapped[str] = mapped_column(String(3), default='INR')
    balance_after_transaction: Mapped[Decimal | None] = mapped_column(Money)
    reference_number: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(40), index=True)
    category_source: Mapped[str] = mapped_column(String(10))  # rule | merchant | llm | manual | default
    category_confidence: Mapped[float] = mapped_column(Float)
    extraction_status: Mapped[str] = mapped_column(String(16))  # parsed | needs_review
    extraction_confidence: Mapped[float] = mapped_column(Float)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---- Investments (Phase 3) --------------------------------------------------------------------

class InvestmentAccount(Base):
    __tablename__ = 'investment_accounts'
    __table_args__ = (UniqueConstraint('user_id', 'platform', 'account_mask'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(30))       # INDMONEY | GROWW | ZERODHA | UNKNOWN
    account_mask: Mapped[str] = mapped_column(String(16))   # last 4 of BO ID / client code only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Security(Base):
    """Shared reference data (not user-specific). ISIN is the identity when known."""
    __tablename__ = 'securities'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(20))     # equity | mutual_fund
    symbol: Mapped[str | None] = mapped_column(String(32))  # market data ticker, e.g. IDEA.NS or AMZN
    currency: Mapped[str] = mapped_column(String(3), default='INR')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InvestmentImport(Base):
    __tablename__ = 'investment_imports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), unique=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey('investment_accounts.id'))
    parser: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(24))           # holdings_snapshot | funds_ledger | mf_orders | unsupported
    status: Mapped[str] = mapped_column(String(24))         # imported | needs_review | unsupported_layout | duplicate
    reconciliation_status: Mapped[str] = mapped_column(String(20))
    reconciliation_json: Mapped[str] = mapped_column(Text, default='{}')
    warnings_json: Mapped[str] = mapped_column(Text, default='[]')
    as_of: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    stated_total_value: Mapped[Decimal | None] = mapped_column(Money)
    summary_json: Mapped[str] = mapped_column(Text, default='{}')  # e.g. realised P&L + charges from a P&L report
    rows_found: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[InvestmentAccount | None] = relationship()


class HoldingPosition(Base):
    """One security in one holdings snapshot, exactly as the statement reported it."""
    __tablename__ = 'holding_positions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    import_id: Mapped[str] = mapped_column(ForeignKey('investment_imports.id'), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('investment_accounts.id'), index=True)
    security_id: Mapped[str] = mapped_column(ForeignKey('securities.id'))
    as_of: Mapped[date] = mapped_column(Date)
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    free_quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    pledged_quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    locked_quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    reported_rate: Mapped[Decimal | None] = mapped_column(Price)
    reported_value: Mapped[Decimal | None] = mapped_column(Money)
    average_cost: Mapped[Decimal | None] = mapped_column(Price)
    cost_value: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default='INR')  # currency of rate/value/cost columns
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_row: Mapped[int | None] = mapped_column(Integer)

    security: Mapped[Security] = relationship()


class BrokerLedgerEntry(Base):
    """Cash movement in a broker's fund ledger (deposits, withdrawals, trade settlements)."""
    __tablename__ = 'broker_ledger_entries'
    __table_args__ = (UniqueConstraint('account_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('investment_accounts.id'), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'))
    import_id: Mapped[str] = mapped_column(ForeignKey('investment_imports.id'))
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    settlement_date: Mapped[date | None] = mapped_column(Date)
    segment_type: Mapped[str] = mapped_column(String(40))
    transaction_type: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))  # deposit | withdrawal | buy_settlement | sell_settlement | charges | other
    reference: Mapped[str | None] = mapped_column(String(64))
    debit: Mapped[Decimal] = mapped_column(Money)
    credit: Mapped[Decimal] = mapped_column(Money)
    balance: Mapped[Decimal | None] = mapped_column(Money)
    source_row: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))


class InvestmentTransaction(Base):
    """A buy/sell of a security (mutual fund orders now; equity tradebooks later)."""
    __tablename__ = 'investment_transactions'
    __table_args__ = (UniqueConstraint('account_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('investment_accounts.id'), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'))
    import_id: Mapped[str] = mapped_column(ForeignKey('investment_imports.id'))
    security_id: Mapped[str] = mapped_column(ForeignKey('securities.id'))
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    transaction_type: Mapped[str] = mapped_column(String(20))  # buy | sell
    quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    price: Mapped[Decimal | None] = mapped_column(Price)
    amount: Mapped[Decimal] = mapped_column(Money)
    source_row: Mapped[int | None] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))

    security: Mapped[Security] = relationship()


# ---- Users, reviews, notifications (Phase 5/6) ------------------------------------------------------

class User(Base):
    """Profile + notification preferences. id is the Firebase uid (or DEMO_USER_ID in dev mode)."""
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_review_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    price_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    price_move_threshold_pct: Mapped[float] = mapped_column(Float, default=5.0)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PortfolioReview(Base):
    __tablename__ = 'portfolio_reviews'
    __table_args__ = (UniqueConstraint('user_id', 'period_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    period_key: Mapped[str] = mapped_column(String(40))   # weekly-2026-W47 | manual-20261121T1015
    status: Mapped[str] = mapped_column(String(12))       # ready | failed
    model: Mapped[str] = mapped_column(String(60))
    inputs_json: Mapped[str] = mapped_column(Text)        # the exact data the report was based on
    report_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Notification(Base):
    __tablename__ = 'notifications'
    __table_args__ = (UniqueConstraint('user_id', 'dedupe_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(24))         # portfolio_review | price_move
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(1000))
    payload_json: Mapped[str] = mapped_column(Text, default='{}')
    dedupe_key: Mapped[str] = mapped_column(String(120))  # makes every alert idempotent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Base):
    __tablename__ = 'notification_deliveries'
    __table_args__ = (UniqueConstraint('notification_id', 'channel'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    notification_id: Mapped[str] = mapped_column(ForeignKey('notifications.id'), index=True)
    channel: Mapped[str] = mapped_column(String(12))      # email
    status: Mapped[str] = mapped_column(String(12))       # pending | sent | failed | skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketFlow(Base):
    """Daily FII/FPI and DII cash-market activity (INR crore). Market-wide, not user data."""
    __tablename__ = 'market_flows'
    __table_args__ = (UniqueConstraint('flow_date', 'category'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flow_date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(8))    # FII | DII
    buy_value: Mapped[Decimal | None] = mapped_column(Money)
    sell_value: Mapped[Decimal | None] = mapped_column(Money)
    net_value: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(40))     # nse | csv_import
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MerchantCategoryOverride(Base):
    """User-configured merchant -> category mapping, created from manual corrections."""
    __tablename__ = 'merchant_category_overrides'
    __table_args__ = (UniqueConstraint('user_id', 'merchant'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    merchant: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
