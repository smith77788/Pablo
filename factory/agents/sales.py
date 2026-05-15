"""
Sales Department — Lead qualification, proposal writing, follow-up, pricing.
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


class LeadQualifierAgent(FactoryAgent):
    department = 'sales'
    role = 'LeadQualifier'
    name = 'Алиса'
    system_prompt = (
        "Ты — Алиса, Lead Qualifier агентства моделей Nevesty Models. "
        "Анализируешь входящие заявки, присваиваешь скоринг и выявляешь высокоценных клиентов. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        orders = _read_db(
            "SELECT event_type, status, COUNT(*) as cnt FROM orders GROUP BY event_type, status"
        )
        data_str = "\n".join(
            f"{r['event_type']} | {r['status']} | {r['cnt']}" for r in orders
        ) if orders else "No orders yet"

        return (
            f"You are {self.name}, Lead Qualifier for a premium modeling agency in Moscow.\n\n"
            "Current orders data by type and status:\n"
            f"{data_str}\n\n"
            "Analyze the new leads/orders and:\n"
            "1. Score each lead type 1-10 for qualification potential\n"
            "2. Identify 3 high-value prospects (high priority)\n"
            "3. Suggest specific follow-up actions for each high-value prospect\n"
            "4. Recommend the optimal follow-up timing for each stage\n\n"
            "Respond in Russian. Be specific and actionable. Max 250 words."
        )

    def run(self, context: dict | None = None) -> dict:
        prompt = self.build_prompt()
        result = self.think(prompt, context=context, max_tokens=600)
        return {'role': self.role, 'department': self.department, 'result': result}


class ProposalWriterAgent(FactoryAgent):
    department = 'sales'
    role = 'ProposalWriter'
    name = 'Михаил'
    system_prompt = (
        "Ты — Михаил, Proposal Writer агентства моделей Nevesty Models. "
        "Создаёшь убедительные персонализированные коммерческие предложения. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        models = _read_db(
            "SELECT name, category, city, featured FROM models "
            "WHERE available=1 ORDER BY featured DESC LIMIT 10"
        )
        model_list = "\n".join(
            f"- {r['name']} ({r['category']}, {r['city']})" for r in models
        ) if models else "No models available"

        orders = _read_db(
            "SELECT event_type, COUNT(*) as cnt FROM orders GROUP BY event_type ORDER BY cnt DESC LIMIT 5"
        )
        event_types = ", ".join(r['event_type'] for r in orders) if orders else "корпоратив, фотосессия"

        return (
            f"You are {self.name}, Proposal Writer for a premium modeling agency.\n\n"
            "Top available models:\n"
            f"{model_list}\n\n"
            f"Most popular event types: {event_types}\n\n"
            "Generate a personalized proposal/pitch template based on client type and event:\n"
            "1. A strong opening value proposition tailored to the event type\n"
            "2. How we select the perfect models for their brand/event\n"
            "3. Our process, timeline, and guarantees\n"
            "4. Pricing structure with packages\n"
            "5. A clear call-to-action\n\n"
            "Write in Russian, professional yet warm tone. Max 300 words."
        )

    def run(self, context: dict | None = None) -> dict:
        prompt = self.build_prompt()
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class FollowUpSpecialistAgent(FactoryAgent):
    department = 'sales'
    role = 'FollowUpSpecialist'
    name = 'Екатерина'
    system_prompt = (
        "Ты — Екатерина, Follow-Up Specialist агентства моделей Nevesty Models. "
        "Отслеживаешь незакрытые заявки и создаёшь персонализированные сообщения для повторного контакта. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        stale_new = _read_db(
            """SELECT COUNT(*) as cnt FROM orders
               WHERE status IN ('new', 'processing')
               AND datetime(created_at) < datetime('now', '-3 days')"""
        )
        cnt_new = stale_new[0]["cnt"] if stale_new else 0

        stale_reviewing = _read_db(
            """SELECT COUNT(*) as cnt FROM orders
               WHERE status = 'reviewing'
               AND datetime(created_at) < datetime('now', '-3 days')"""
        )
        cnt_reviewing = stale_reviewing[0]["cnt"] if stale_reviewing else 0

        return (
            f"You are {self.name}, Follow-Up Specialist for a modeling agency.\n\n"
            f"Stale orders (new/processing > 3 days old): {cnt_new}\n"
            f"Stale orders (reviewing > 3 days old): {cnt_reviewing}\n\n"
            "Create 3 follow-up message templates for:\n"
            "1. A client who submitted a new/processing request 3+ days ago and needs follow-up\n"
            "2. A client whose order is in 'reviewing' status for 3+ days\n"
            "3. A past client (completed order 3+ months ago) for re-engagement\n\n"
            "Each template should:\n"
            "- Be in Russian, warm but professional tone\n"
            "- Be under 100 words\n"
            "- Include a specific call-to-action\n"
            "- Feel personal, not like a generic template\n\n"
            "Format: Шаблон 1: ... / Шаблон 2: ... / Шаблон 3: ..."
        )

    def run(self, context: dict | None = None) -> dict:
        prompt = self.build_prompt()
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class PricingNegotiatorAgent(FactoryAgent):
    department = 'sales'
    role = 'PricingNegotiator'
    name = 'Дмитрий'
    system_prompt = (
        "Ты — Дмитрий, Pricing Negotiator агентства моделей Nevesty Models. "
        "Анализируешь распределение бюджетов и предлагаешь оптимальную стратегию ценообразования. "
        "Всё на русском языке."
    )

    def build_prompt(self) -> str:
        budget_data = _read_db(
            """SELECT event_type, status, COUNT(*) as cnt
               FROM orders
               GROUP BY event_type, status
               ORDER BY cnt DESC LIMIT 20"""
        )
        budget_str = "\n".join(
            f"{r['event_type']} | {r['status']} | {r['cnt']} заявок"
            for r in budget_data
        ) if budget_data else "No budget data available"

        return (
            f"You are {self.name}, Pricing Negotiator for a premium modeling agency.\n\n"
            "Order distribution by event type and status:\n"
            f"{budget_str}\n\n"
            "Analyze the budget distribution and suggest pricing strategy:\n"
            "1. Assess which event types command premium pricing\n"
            "2. Suggest 3 pricing packages (budget/standard/premium)\n"
            "3. Recommend discount thresholds and conditions\n"
            "4. Identify negotiation scripts for common price objections\n"
            "5. Suggest seasonal pricing adjustments\n\n"
            "Respond in Russian. Be data-driven and specific. Max 300 words."
        )

    def run(self, context: dict | None = None) -> dict:
        prompt = self.build_prompt()
        result = self.think(prompt, context=context, max_tokens=700)
        return {'role': self.role, 'department': self.department, 'result': result}


class LeadQualifier(LeadQualifierAgent):
    """Alias with canonical name for test compatibility."""
    name = 'LeadQualifier'


class ProposalWriter(ProposalWriterAgent):
    """Alias with canonical name for test compatibility."""
    name = 'ProposalWriter'


class FollowUpSpecialist(FollowUpSpecialistAgent):
    """Alias with canonical name for test compatibility."""
    name = 'FollowUpSpecialist'


class PricingNegotiator(PricingNegotiatorAgent):
    """Alias with canonical name for test compatibility."""
    name = 'PricingNegotiator'


class SalesDepartment:
    """Sales Department: 4 agents for lead qualification, proposals, follow-up, pricing."""

    def __init__(self):
        self.agents = [
            LeadQualifierAgent(),
            ProposalWriterAgent(),
            FollowUpSpecialistAgent(),
            PricingNegotiatorAgent(),
        ]

    def run_cycle(self) -> dict:
        results = {}
        for agent in self.agents:
            try:
                results[agent.role] = agent.run()  # type: ignore[attr-defined]
            except Exception as e:
                results[agent.role] = {"error": str(e)}
        return results
