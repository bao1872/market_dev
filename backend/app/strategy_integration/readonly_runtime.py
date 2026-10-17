"""Strategy-only connection setup; the application's shared DB owner is unchanged."""
from __future__ import annotations

import os

_installed = False


def configure_readonly_connections():
    global _installed
    if os.getenv('PANJI_STRATEGY_READ_ONLY_DATABASE') != '1' or _installed:
        return
    from sqlalchemy import event

    from app.db import async_engine

    @event.listens_for(async_engine.sync_engine, 'connect')
    def set_readonly(dbapi_connection, _record):
        # Execute on the native asyncpg connection before SQLAlchemy begins an
        # application transaction, so a pool rollback cannot undo this setting.
        dbapi_connection.run_async(lambda connection: connection.execute(
            'SET default_transaction_read_only = on'))

    _installed = True


async def assert_readonly_session(session):
    if os.getenv('PANJI_STRATEGY_READ_ONLY_DATABASE') != '1':
        return False
    from sqlalchemy import text
    result = await session.execute(text('SHOW transaction_read_only'))
    if result.scalar_one() != 'on':
        raise RuntimeError('STRATEGY_DATABASE_NOT_READ_ONLY')
    return True
