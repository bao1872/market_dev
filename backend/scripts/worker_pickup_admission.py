#!/usr/bin/env python3
"""Operator / deploy 操作面：worker pickup admission（E2.1 P1-C）。

所有 pause / status / release 调用 canonical admission service；
禁止 shell / 其它入口直接 UPDATE 表（E2.1 §G）。

子命令：
  acquire     --scope S --actor A [--reason R]
              获取本次 deploy/operator 自己的 pause；输出 {"acquired":true,"token":...}
              若已被他人/先前 pause 持有（不同 token），退出码非 0（调用方不得借用）。
  status      --scope S
              读取状态 JSON（installed/paused/pause_token/paused_by/reason/paused_at）。
  release     --scope S --token T
              仅当 token 匹配时释放 own pause；否则退出码非 0。
  verify-own  --scope S --token T
              校验 own pause 仍有效（paused 且 token 匹配）；退出码 0 表示仍拥有。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db import AsyncSessionLocal
from app.services.worker_pickup_admission_service import (
    acquire_pause,
    get_status,
    new_pause_token,
    release_pause,
)


async def _acquire(scope: str, actor: str, reason: str | None) -> int:
    token = new_pause_token()
    async with AsyncSessionLocal() as db:
        ok = await acquire_pause(
            db, scope=scope, token=token, actor=actor, reason=reason
        )
        if not ok:
            st = await get_status(db, scope)
            print(
                json.dumps(
                    {
                        "acquired": False,
                        "paused": st.paused,
                        "pause_token": st.pause_token,
                        "paused_by": st.paused_by,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        await db.commit()
    print(
        json.dumps({"acquired": True, "token": token, "paused": True}, ensure_ascii=False)
    )
    return 0


async def _status(scope: str) -> int:
    from sqlalchemy.exc import ProgrammingError

    try:
        async with AsyncSessionLocal() as db:
            st = await get_status(db, scope)
    except ProgrammingError:
        # 表尚未安装（如 first-install bootstrap 前）：installed=false，不视为错误。
        # 部署侧 _admission_installed 据此跳过 steady-state acquire，先跑 migration 093。
        print(
            json.dumps(
                {
                    "installed": False,
                    "paused": False,
                    "pause_token": None,
                    "paused_by": None,
                    "reason": None,
                    "paused_at": None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "installed": st.installed,
                "paused": st.paused,
                "pause_token": st.pause_token,
                "paused_by": st.paused_by,
                "reason": st.reason,
                "paused_at": st.paused_at.isoformat() if st.paused_at else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _release(scope: str, token: str) -> int:
    async with AsyncSessionLocal() as db:
        ok = await release_pause(db, scope=scope, token=token)
        await db.commit()
    print(json.dumps({"released": ok}, ensure_ascii=False))
    return 0 if ok else 1


async def _verify_own(scope: str, token: str) -> int:
    async with AsyncSessionLocal() as db:
        st = await get_status(db, scope)
    owned = bool(st.paused and st.pause_token == token)
    print(
        json.dumps(
            {
                "owned": owned,
                "installed": st.installed,
                "paused": st.paused,
                "pause_token": st.pause_token,
            },
            ensure_ascii=False,
        )
    )
    return 0 if owned else 1


def main() -> None:
    p = argparse.ArgumentParser(description="worker pickup admission operator surface")
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("acquire")
    pa.add_argument("--scope", required=True)
    pa.add_argument("--actor", required=True)
    pa.add_argument("--reason", default=None)

    ps = sub.add_parser("status")
    ps.add_argument("--scope", required=True)

    pr = sub.add_parser("release")
    pr.add_argument("--scope", required=True)
    pr.add_argument("--token", required=True)

    pv = sub.add_parser("verify-own")
    pv.add_argument("--scope", required=True)
    pv.add_argument("--token", required=True)

    args = p.parse_args()
    if args.cmd == "acquire":
        rc = asyncio.run(_acquire(args.scope, args.actor, args.reason))
    elif args.cmd == "status":
        rc = asyncio.run(_status(args.scope))
    elif args.cmd == "release":
        rc = asyncio.run(_release(args.scope, args.token))
    else:  # verify-own
        rc = asyncio.run(_verify_own(args.scope, args.token))
    sys.exit(rc)


if __name__ == "__main__":
    main()
