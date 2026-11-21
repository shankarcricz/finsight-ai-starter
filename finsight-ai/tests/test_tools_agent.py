"""Agent tool integration without any network: a scripted fake chat model drives the real agent graph."""
import json
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError
from app.agent import make_agent
from app.models import Document
from app.statement_import import import_statement
from app.tools import make_finance_tools
from tests.conftest import USER


@pytest.fixture
def imported(session, demo_layout_pages):
    doc = Document(user_id=USER, category='bank', filename='s.pdf', sha256='f' * 64, pages=2)
    session.add(doc)
    session.flush()
    import_statement(session, user_id=USER, document_id=doc.id, layout_pages=demo_layout_pages)
    session.commit()


def tools_by_name(user=USER):
    return {t.name: t for t in make_finance_tools(user)}


def test_tool_registry_has_no_user_id_parameter():
    for t in make_finance_tools(USER):
        assert 'user_id' not in t.args, t.name  # identity is bound server-side, not chosen by the LLM


def test_spending_summary_tool_returns_exact_strings(imported):
    out = json.loads(tools_by_name()['get_spending_summary'].invoke({'start_date': '2026-01-01', 'end_date': '2026-12-31'}))
    assert out['income'] == '960000.00' and out['spending'] == '267100.00'
    assert out['meta']['statements'][0]['reconciliation_status'] == 'reconciled'


def test_category_and_transaction_tools(imported):
    tools = tools_by_name()
    cats = json.loads(tools['get_category_spending'].invoke({}))
    assert cats['categories'][0] == {'category': 'Rent', 'amount': '198000.00', 'count': 11, 'share_pct': '74.13'}
    txns = json.loads(tools['get_bank_transactions'].invoke({'category': 'Travel'}))
    assert txns['total_matching'] == 2 and txns['sum_debits'] == '22300.00'
    status = json.loads(tools['get_statement_import_status'].invoke({}))
    assert status['bank_imports'][0]['reconciliation_status'] == 'reconciled'
    assert status['investment_imports'] == []


def test_tools_enforce_user_isolation(imported):
    out = json.loads(tools_by_name('intruder')['get_spending_summary'].invoke({}))
    assert out['income'] == '0.00' and out['meta']['transactions_considered'] == 0


@pytest.mark.parametrize('name,args', [
    ('get_spending_summary', {'start_date': '2026-12-31', 'end_date': '2026-01-01'}),  # reversed range
    ('get_spending_summary', {'start_date': 'last tuesday'}),
    ('get_bank_transactions', {'category': 'Crypto'}),                               # not a known category
    ('get_bank_transactions', {'limit': 10_000}),
    ('get_spending_trends', {'month': '2026-13'}),
])
def test_tool_inputs_are_validated(name, args):
    with pytest.raises(ValidationError):
        tools_by_name()[name].invoke(args)


class ScriptedModel(GenericFakeChatModel):
    """Replays fixed AIMessages; accepts bind_tools so create_agent can use it."""
    def bind_tools(self, tools, **kwargs):
        return self


def test_agent_calls_structured_tool_and_answers(imported):
    model = ScriptedModel(messages=iter([
        AIMessage(content='', tool_calls=[{'name': 'get_category_spending', 'id': 'call_1',
                                           'args': {'start_date': '2026-01-01', 'end_date': '2026-11-30'}}]),
        AIMessage(content='You spent INR 198000.00 on rent.'),
    ]))
    result = make_agent(USER, model=model).invoke({'messages': [{'role': 'user', 'content': 'How much on rent?'}]})
    tool_msgs = [m for m in result['messages'] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1 and tool_msgs[0].name == 'get_category_spending'
    assert json.loads(tool_msgs[0].content)['categories'][0]['amount'] == '198000.00'
    assert result['messages'][-1].content == 'You spent INR 198000.00 on rent.'


def test_agent_registers_rag_and_structured_tools():
    model = ScriptedModel(messages=iter([AIMessage(content='hi')]))
    graph = make_agent(USER, model=model)
    names = set(graph.nodes['tools'].bound._tools_by_name)
    assert {'search_uploaded_documents', 'get_spending_summary', 'get_recurring_payments',
            'get_bank_transactions', 'get_portfolio_cost_basis'} <= names
