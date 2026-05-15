"""🌐 Factory API — FastAPI dashboard endpoints."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from factory import db
from factory.cycle import run_cycle

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Startup Factory", version="1.0.0")


# ── Models ─────────────────────────────────────────────────────────────────────

class CycleResponse(BaseModel):
    cycle_id: str
    health_score: int
    summary: str
    phases: dict
    elapsed_s: float


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/factory/status")
def factory_status():
    """Summary of the latest cycle + active products."""
    last_cycle = db.fetch_one(
        "SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1"
    )
    active_products = db.get_active_products()
    running_experiments = db.get_running_experiments()
    pending_actions = db.get_pending_growth_actions(10)
    recent_decisions = db.get_recent_decisions(5)

    return {
        "last_cycle": last_cycle,
        "active_products": active_products,
        "running_experiments": running_experiments,
        "pending_actions": pending_actions,
        "recent_decisions": recent_decisions,
    }


@app.post("/factory/cycle/run")
def trigger_cycle():
    """Manually trigger a factory cycle."""
    try:
        result = run_cycle()
        return result
    except Exception as e:
        logger.error("Manual cycle error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/factory/products")
def list_products(status: str | None = None):
    if status:
        rows = db.fetch_all("SELECT * FROM products WHERE status=? ORDER BY created_at DESC", (status,))
    else:
        rows = db.fetch_all("SELECT * FROM products ORDER BY created_at DESC")
    return rows


@app.get("/factory/products/{product_id}")
def get_product(product_id: int):
    row = db.fetch_one("SELECT * FROM products WHERE id=?", (product_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    metrics = db.get_product_metrics(product_id, 30)
    experiments = db.fetch_all("SELECT * FROM experiments WHERE product_id=? ORDER BY started_at DESC", (product_id,))
    actions = db.fetch_all("SELECT * FROM growth_actions WHERE product_id=? ORDER BY created_at DESC LIMIT 20", (product_id,))
    return {"product": row, "metrics": metrics, "experiments": experiments, "actions": actions}


@app.get("/factory/ideas")
def list_ideas(status: str = "new"):
    return db.fetch_all("SELECT * FROM ideas WHERE status=? ORDER BY priority DESC", (status,))


@app.get("/factory/experiments")
def list_experiments(status: str | None = None):
    if status:
        return db.fetch_all("SELECT * FROM experiments WHERE status=? ORDER BY started_at DESC", (status,))
    return db.fetch_all("SELECT * FROM experiments ORDER BY started_at DESC LIMIT 50")


@app.get("/factory/growth-actions")
def list_growth_actions(status: str = "pending", limit: int = 20):
    return db.fetch_all(
        "SELECT * FROM growth_actions WHERE status=? ORDER BY priority DESC, created_at DESC LIMIT ?",
        (status, limit),
    )


@app.patch("/factory/growth-actions/{action_id}/done")
def mark_action_done(action_id: int):
    db.execute(
        "UPDATE growth_actions SET status='done', updated_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), action_id),
    )
    return {"ok": True}


@app.get("/factory/cycles")
def list_cycles(limit: int = 20):
    return db.fetch_all("SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,))


@app.get("/factory/decisions")
def list_decisions(limit: int = 20):
    return db.fetch_all("SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))


@app.get("/factory/content")
def get_content():
    """Get latest content generation results."""
    try:
        from factory.agents.content_generator import ContentGenerator
        gen = ContentGenerator()
        result = gen.run()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
