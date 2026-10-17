"""Pure connection/session adapter contracts; never opens a database."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.strategy_integration import readonly_runtime as readonly


def test_readonly_hook_installed_once_and_uses_native_connection(monkeypatch):
    listeners, statements = [], []
    monkeypatch.setenv('PANJI_STRATEGY_READ_ONLY_DATABASE', '1')
    monkeypatch.setattr(readonly, '_installed', False)
    def register(target, name):
        assert name == 'connect'
        def decorator(callback):
            listeners.append(callback)
            return callback
        return decorator
    monkeypatch.setattr(event, 'listens_for', register)
    readonly.configure_readonly_connections()
    readonly.configure_readonly_connections()
    assert len(listeners) == 1
    async def execute(statement):
        statements.append(statement)
    adapter = SimpleNamespace(run_async=lambda fn: asyncio.run(fn(SimpleNamespace(execute=execute))))
    listeners[0](adapter, None)
    assert statements == ['SET default_transaction_read_only = on']


@pytest.mark.parametrize('value,accepted', [('on', True), ('off', False)])
def test_real_session_readonly_flag_is_required(monkeypatch, value, accepted):
    monkeypatch.setenv('PANJI_STRATEGY_READ_ONLY_DATABASE', '1')
    async def execute(statement):
        assert str(statement) == 'SHOW transaction_read_only'
        return SimpleNamespace(scalar_one=lambda: value)
    session = SimpleNamespace(execute=execute)
    if accepted:
        assert asyncio.run(readonly.assert_readonly_session(session))
    else:
        with pytest.raises(RuntimeError, match='STRATEGY_DATABASE_NOT_READ_ONLY'):
            asyncio.run(readonly.assert_readonly_session(session))


def test_mdas_input_uses_stock_owner_and_limits_before_read(monkeypatch):
    from datetime import date

    from app.models.instrument import Instrument
    from app.services.instrument_maintenance_service import stock_symbol_sql_filter
    from app.strategy_integration import db_export
    seen = []
    monkeypatch.delenv('PANJI_STRATEGY_READ_ONLY_DATABASE', raising=False)
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def execute(self, statement):
            assert statement._where_criteria[0].compare(stock_symbol_sql_filter(Instrument))
            assert statement._limit_clause.value == 25
            return SimpleNamespace(all=lambda: [SimpleNamespace(id='i1', symbol='600000', market='SH')])
    async def mdas(db, instruments, as_of, **kwargs):
        seen.append(instruments)
        return {'SH:600000': object()}, {'eligible_stock_count': 1}
    monkeypatch.setattr(db_export, 'AsyncSessionLocal', Session)
    monkeypatch.setattr(db_export, 'load_official_qfq_stock_batch', mdas)
    _, report = asyncio.run(db_export.read_official_qfq_stock_batch(date(2026,10,16), max_symbols=25))
    assert seen == [{'i1': 'SH:600000'}]
    assert report['connection_read_only_verified'] is False
