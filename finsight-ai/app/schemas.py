"""Pydantic contracts for structured financial data (validated before anything touches SQL)."""
from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

Category = Literal[
    'Salary', 'Food', 'Groceries', 'Rent', 'Utilities', 'Shopping', 'Entertainment', 'Healthcare',
    'Transport', 'Travel', 'Subscriptions', 'Investment transfers', 'Internal transfers',
    'Loan repayments', 'Fees', 'Interest', 'Refunds', 'Other',
]
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]

# Movements of the user's own money: neither income nor spending.
NON_SPENDING_CATEGORIES = frozenset({'Investment transfers', 'Internal transfers'})


class BankTransaction(BaseModel):
    transaction_date: date
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0, decimal_places=2)
    transaction_type: Literal['credit', 'debit']
    category: Category | None = None
    balance_after_transaction: Decimal | None = Field(default=None, decimal_places=2)
    reference_number: str | None = Field(default=None, max_length=64)

    @field_validator('description')
    @classmethod
    def _strip(cls, v: str) -> str:
        return ' '.join(v.split())


class ParsedTransaction(BankTransaction):
    """A transaction plus extraction provenance."""
    original_description: str
    source_page: int
    source_row: int
    extraction_confidence: float = Field(ge=0, le=1)
    extraction_status: Literal['parsed', 'needs_review'] = 'parsed'


class StatementSummary(BaseModel):
    institution: str = 'Unknown bank'
    account_identifier: str | None = None
    currency: str = 'INR'
    period_start: date | None = None
    period_end: date | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    total_credits: Decimal | None = None
    total_debits: Decimal | None = None


class ExtractionWarning(BaseModel):
    code: str
    message: str
    page: int | None = None


class ParsedStatement(BaseModel):
    parser: str
    summary: StatementSummary
    transactions: list[ParsedTransaction]
    warnings: list[ExtractionWarning] = []


# ---- Investments ------------------------------------------------------------------------------

Platform = Literal['INDMONEY', 'GROWW', 'ZERODHA', 'UNKNOWN']
ISIN_PATTERN = r'^IN[A-Z0-9]{9}\d$'


class HoldingRow(BaseModel):
    isin: str | None = Field(default=None, pattern=ISIN_PATTERN)   # Indian securities
    symbol: str | None = Field(default=None, pattern=r'^[A-Z0-9.\-^=]{1,20}$')  # e.g. AMZN for US stocks
    currency: str = Field(default='INR', pattern=r'^[A-Z]{3}$')
    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(ge=0)
    free_quantity: Decimal | None = None
    pledged_quantity: Decimal | None = None
    demat_quantity: Decimal | None = None
    remat_quantity: Decimal | None = None
    locked_quantity: Decimal | None = None
    reported_rate: Decimal | None = Field(default=None, ge=0)
    reported_value: Decimal | None = Field(default=None, ge=0)
    average_cost: Decimal | None = Field(default=None, ge=0)   # broker-reported average buy price
    cost_value: Decimal | None = Field(default=None, ge=0)     # broker-reported buy value
    source_page: int
    source_row: int

    @model_validator(mode='after')
    def _identified(self):
        if not (self.isin or self.symbol):
            raise ValueError('A holding needs an ISIN or a ticker symbol')
        return self


class ParsedHoldings(BaseModel):
    parser: str
    platform: Platform
    account_ref: str | None  # masked, last 4 only
    as_of: date | None
    stated_total_value: Decimal | None
    stated_cost_total: Decimal | None = None
    rows: list[HoldingRow]
    warnings: list[ExtractionWarning] = []


class LotRow(BaseModel):
    """One lot from a broker P&L report: realised (bought + sold) or still open."""
    isin: str = Field(pattern=ISIN_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0)
    buy_date: date
    buy_price: Decimal = Field(ge=0)
    buy_value: Decimal = Field(ge=0)
    sell_date: date | None = None       # None for open lots
    sell_price: Decimal | None = None   # closing price for open lots
    sell_value: Decimal | None = None   # closing value for open lots
    pnl: Decimal
    realised: bool
    source_row: int


class ParsedTradeReport(BaseModel):
    parser: str
    platform: Platform
    account_ref: str | None
    period_start: date | None
    period_end: date | None
    stated_realised_pnl: Decimal | None
    stated_unrealised_pnl: Decimal | None
    charges: dict[str, Decimal]
    lots: list[LotRow]
    warnings: list[ExtractionWarning] = []


LedgerKind = Literal['deposit', 'withdrawal', 'buy_settlement', 'sell_settlement', 'charges', 'other']


class LedgerRow(BaseModel):
    entry_date: date
    settlement_date: date | None
    segment_type: str
    transaction_type: str
    kind: LedgerKind
    reference: str | None
    debit: Decimal = Field(ge=0, decimal_places=2)
    credit: Decimal = Field(ge=0, decimal_places=2)
    balance: Decimal | None = Field(default=None, decimal_places=2)
    source_row: int


class ParsedLedger(BaseModel):
    parser: str
    platform: Platform
    account_ref: str | None
    period_start: date | None
    period_end: date | None
    rows: list[LedgerRow]  # chronological
    warnings: list[ExtractionWarning] = []


class FundOrderRow(BaseModel):
    scheme_name: str = Field(min_length=1, max_length=200)
    transaction_type: Literal['buy', 'sell']
    units: Decimal | None = Field(default=None, gt=0)
    nav: Decimal | None = Field(default=None, gt=0)
    amount: Decimal = Field(gt=0, decimal_places=2)
    order_date: date
    source_row: int


class ParsedFundOrders(BaseModel):
    parser: str
    platform: Platform
    account_ref: str | None
    period_start: date | None
    period_end: date | None
    rows: list[FundOrderRow]
    warnings: list[ExtractionWarning] = []
