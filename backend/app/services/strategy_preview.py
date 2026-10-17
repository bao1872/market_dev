"""Request-scoped, admin-only selection of an immutable validated batch."""
import json
import os
import re
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from app.services.access_control_service import require_authenticated
from app.strategy_integration.release_store import preview_batch


async def select_preview(request: Request, ctx=Depends(require_authenticated)):
    batch_id = request.query_params.get('preview_batch')
    token = None
    if batch_id is not None:
        if not ctx.is_admin:
            raise HTTPException(403, '候选批次仅限管理员预览')
        if not re.fullmatch(r'[0-9a-f]{64}', batch_id):
            raise HTTPException(422, '候选批次身份不合法')
        root = os.getenv('PANJI_STRATEGY_RELEASE_ROOT', '')
        if not root or not Path(root).is_absolute():
            raise HTTPException(503, '策略批次目录未配置')
        from app.strategy_integration.release_store import PublishError, resolve_preview_batch
        try:
            batch = resolve_preview_batch(Path(root), batch_id)
        except (PublishError, ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            raise HTTPException(409, '候选批次未完成技术验证或身份不一致') from exc
        token = preview_batch.set(batch)
    try:
        yield
    finally:
        if token is not None:
            preview_batch.reset(token)
