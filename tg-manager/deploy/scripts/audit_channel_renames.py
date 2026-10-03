"""Read-only incident report for recent channel rename operations."""
from __future__ import annotations

import asyncio
import json
import os

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            "SELECT id,owner_id,op_type,status,created_at,finished_at,params,result "
            "FROM operation_queue WHERE (op_type='bulk_seo_apply' OR ("
            "op_type IN ('bulk_edit_channels','bulk_chan_exec') "
            "AND (params->>'field'='title' OR params->>'op'='chan_title'))) "
            "ORDER BY id DESC LIMIT 100"
        )
        report = []
        for row in rows:
            params = row["params"] if isinstance(row["params"], dict) else json.loads(row["params"] or "{}")
            result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
            pairs = params.get("channel_acc_pairs") or []
            report.append({
                "id": row["id"], "owner_id": row["owner_id"], "op_type": row["op_type"],
                "status": row["status"], "created_at": str(row["created_at"]),
                "finished_at": str(row["finished_at"]), "field": params.get("field") or params.get("op"),
                "value": params.get("value"), "accounts": len(params.get("account_ids") or []),
                "channels": len(pairs) or len(params.get("channel_ids") or []),
                "channel_ids": (params.get("channel_ids") or [])[:10],
                "previous_titles": [p.get("title") for p in pairs[:5]],
                "ok": result.get("ok"), "failed": result.get("failed") or result.get("fail"),
            })
        print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        await conn.close()


asyncio.run(main())
