"""
Sales Department — Lead qualification, proposal writing, follow-up.
Reads live orders data from nevesty-models DB.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from factory.agents.base import FactoryAgent

DB_PATH = Path(__file__).parent.parent.parent / "nevesty-models" / "data.db"


def _read_db(query: str, params=()) -> list[dict]:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


class LeadQualifier(FactoryAgent):
    name = "LeadQualifier"
    department = "sales"
    role = "lead_qualifier_db"
    system_prompt = (
        "Ты — Sales Lead Qualifier агентства моделей Nevesty Models. "
        "Анализируешь данные заявок из базы и даёшь конкретные рекомендации. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        orders = _read_db(
            "SELECT event_type, status, COUNT(*) as cnt FROM orders GROUP BY event_type, status"
        )
        data_str = "\n".join(
            f"{r['event_type']} | {r['status']} | {r['cnt']}" for r in orders
        )

        return (
            "You are a Sales Lead Qualifier for a premium modeling agency in Moscow.\n\n"
            "Current orders data by type and status:\n"
            f"{data_str or 'No orders yet'}\n\n"
            "Analyze the order pipeline and:\n"
            "1. Identify which event types have highest conversion rates\n"
            "2. Identify bottlenecks in the pipeline (where leads get stuck)\n"
            "3. Suggest 3 specific qualification criteria to prioritize high-value leads\n"
            "4. Recommend the optimal follow-up timing for each stage\n\n"
            "Be specific, data-driven, and practical. Max 250 words."
        )

    def run(self, context: dict | None = None) -> dict:
        try:
            prompt = self.build_prompt()
            result = self.think(prompt, context=context, max_tokens=600)
            return {"agent": self.name, "output": result}
        except Exception as e:
            return {"agent": self.name, "error": str(e)}


class ProposalWriter(FactoryAgent):
    name = "ProposalWriter"
    department = "sales"
    role = "proposal_writer_db"
    system_prompt = (
        "Ты — Proposal Writer агентства моделей Nevesty Models. "
        "Создаёшь убедительные коммерческие предложения на основе реального каталога моделей. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        models = _read_db(
            "SELECT name, category, city, featured FROM models "
            "WHERE available=1 ORDER BY featured DESC LIMIT 10"
        )
        model_list = "\n".join(
            f"- {r['name']} ({r['category']}, {r['city']})" for r in models
        )

        return (
            "You are a Sales Proposal Writer for a premium modeling agency.\n\n"
            "Top available models:\n"
            f"{model_list or 'No models available'}\n\n"
            "Create a compelling proposal template for a fashion brand seeking models for:\n"
            "- A 2-day photo shoot in Moscow\n"
            "- 3-5 models needed\n"
            "- Budget: 150,000-300,000 rubles\n\n"
            "Include:\n"
            "1. A strong opening value proposition\n"
            "2. How we select the perfect models for their brand\n"
            "3. Our process and guarantees\n"
            "4. A clear call-to-action\n\n"
            "Write in Russian, professional tone. Max 300 words."
        )

    def run(self, context: dict | None = None) -> dict:
        try:
            prompt = self.build_prompt()
            result = self.think(prompt, context=context, max_tokens=700)
            return {"agent": self.name, "output": result}
        except Exception as e:
            return {"agent": self.name, "error": str(e)}


class FollowUpSpecialist(FactoryAgent):
    name = "FollowUpSpecialist"
    department = "sales"
    role = "followup_specialist_db"
    system_prompt = (
        "Ты — Follow-Up Specialist агентства моделей Nevesty Models. "
        "Отслеживаешь незакрытые заявки и создаёшь персонализированные шаблоны сообщений. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        stale_orders = _read_db(
            """SELECT COUNT(*) as cnt FROM orders
               WHERE status = 'reviewing'
               AND datetime(created_at) < datetime('now', '-2 days')"""
        )
        cnt = stale_orders[0]["cnt"] if stale_orders else 0

        return (
            f"You are a Follow-Up Specialist for a modeling agency.\n\n"
            f"There are currently {cnt} orders in 'reviewing' status for more than 2 days.\n\n"
            "Create 3 follow-up message templates for:\n"
            "1. A client who submitted a request 2 days ago and hasn't heard back\n"
            "2. A client who confirmed a booking but hasn't provided final details\n"
            "3. A past client (completed order 3+ months ago) for re-engagement\n\n"
            "Each template should:\n"
            "- Be in Russian, warm but professional tone\n"
            "- Be under 100 words\n"
            "- Include a specific call-to-action\n"
            "- Not feel like a template\n\n"
            "Format: Template 1: ... / Template 2: ... / Template 3: ..."
        )

    def run(self, context: dict | None = None) -> dict:
        try:
            prompt = self.build_prompt()
            result = self.think(prompt, context=context, max_tokens=700)
            return {"agent": self.name, "output": result}
        except Exception as e:
            return {"agent": self.name, "error": str(e)}


class SalesDepartment:
    """DB-aware Sales Department: reads live data from nevesty-models."""

    def __init__(self):
        self.agents = [LeadQualifier(), ProposalWriter(), FollowUpSpecialist()]

    def run_cycle(self) -> dict:
        results = {}
        for agent in self.agents:
            try:
                results[agent.name] = agent.run()
            except Exception as e:
                results[agent.name] = {"error": str(e)}
        return results
