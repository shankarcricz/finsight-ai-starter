from types import SimpleNamespace
import pytest
from app.categorization import categorize, needs_category_review, rule_category


@pytest.mark.parametrize('desc,direction,expected', [
    ('SALARY CREDIT - ACME TECH', 'credit', 'Salary'),
    ('BONUS CREDIT - ACME TECH', 'credit', 'Salary'),
    ('HOUSE RENT - UPI', 'debit', 'Rent'),
    ('GROCERY MART - DEBIT CARD', 'debit', 'Groceries'),
    ('ELECTRICITY BILL - UPI', 'debit', 'Utilities'),
    ('SIP - NIFTY 50 INDEX FUND', 'debit', 'Investment transfers'),
    ('NEFT TO ZERODHA BROKING', 'debit', 'Investment transfers'),
    ('UPI/1234/GROWW/groww@axis', 'debit', 'Investment transfers'),
    ('RESTAURANT - UPI', 'debit', 'Food'),
    ('UPI/402112345678/SWIGGY', 'debit', 'Food'),
    ('ONLINE SHOPPING', 'debit', 'Shopping'),
    ('HOSPITAL CHECKUP', 'debit', 'Healthcare'),
    ('TRAVEL BOOKING', 'debit', 'Travel'),
    ('NETFLIX.COM SUBSCRIPTION', 'debit', 'Subscriptions'),
    ('SELF TRANSFER TO SAVINGS', 'debit', 'Internal transfers'),
    ('AMAZON REFUND', 'credit', 'Refunds'),
    ('SB INT CREDIT', 'credit', 'Interest'),
    ('HOME LOAN EMI', 'debit', 'Loan repayments'),
])
def test_rules(desc, direction, expected):
    assert rule_category(desc, direction) == expected


def test_direction_specific_rules():
    assert rule_category('SALARY ADVANCE REPAYMENT', 'debit') is None  # salary rule is credit-only
    assert rule_category('HOUSE RENT', 'credit') is None             # rent received is not rent spending


def t(desc, kind='debit'):
    return SimpleNamespace(description=desc, transaction_type=kind)


def test_merchant_override_beats_rules():
    [r] = categorize([t('ONLINE SHOPPING')], overrides={'ONLINE SHOPPING': 'Entertainment'})
    assert (r.category, r.source, r.confidence) == ('Entertainment', 'merchant', 1.0)


def test_unknown_without_llm_defaults_to_other_for_review():
    [r] = categorize([t('LAPTOP REPAIR')])
    assert (r.category, r.source) == ('Other', 'default') and needs_category_review(r)


def test_llm_only_sees_unmatched_transactions():
    seen = []

    def fake_llm(items):
        seen.extend(items)
        return {i['index']: ('Shopping', 0.82) for i in items}

    results = categorize([t('HOUSE RENT'), t('LAPTOP REPAIR')], llm_classifier=fake_llm)
    assert [i['description'] for i in seen] == ['LAPTOP REPAIR']
    assert (results[0].category, results[0].source) == ('Rent', 'rule')
    assert (results[1].category, results[1].source) == ('Shopping', 'llm')
    assert not needs_category_review(results[1])


def test_low_confidence_llm_result_needs_review():
    [r] = categorize([t('ZXQ 7781')], llm_classifier=lambda items: {0: ('Shopping', 0.3)})
    assert needs_category_review(r)


def test_llm_failure_never_blocks_categorization():
    def broken(items):
        raise RuntimeError('OpenAI down')

    [r] = categorize([t('LAPTOP REPAIR')], llm_classifier=broken)
    assert (r.category, r.source) == ('Other', 'default')
