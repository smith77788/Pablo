"""⚙️ Operations Department — оптимизация процессов, автоматизация, CRM, планирование задач."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class WorkflowManager(FactoryAgent):
    department = "operations"
    role = "workflow_manager"
    name = "workflow_manager"
    system_prompt = """Ты — Workflow Manager агентства моделей Nevesty Models.
Твоя задача — оптимизировать бизнес-процессы агентства: от заявки клиента до выполнения заказа.
Анализируешь узкие места в цепочке: заявка → подбор модели → согласование → мероприятие → оплата.
Предлагаешь конкретные улучшения процессов. Всё на русском языке."""

    def optimize_workflow(self, context: dict) -> dict:
        """Анализирует процессы и предлагает оптимизацию."""
        try:
            return self.think_json(
                "Проанализируй текущие бизнес-процессы агентства моделей и предложи оптимизацию.\n"
                "Верни JSON:\n"
                '{"bottlenecks": ["узкое место 1", "узкое место 2"], '
                '"improvements": [{"process": "...", "current": "...", "optimized": "...", "impact": "высокий|средний|низкий"}], '
                '"quick_wins": ["быстрое улучшение 1", "быстрое улучшение 2"], '
                '"estimated_time_saved_hrs": 5}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[operations/workflow_manager] optimize_workflow error: %s", e)
            return {}


class AutomationBuilder(FactoryAgent):
    department = "operations"
    role = "automation_builder"
    name = "automation_builder"
    system_prompt = """Ты — Automation Builder агентства моделей Nevesty Models.
Находишь рутинные задачи, которые можно автоматизировать с помощью Telegram-бота и скриптов.
Знаешь Node.js, Python, Telegram Bot API. Предлагаешь конкретные технические решения.
Всё на русском языке."""

    def find_automation_opportunities(self, context: dict) -> dict:
        """Определяет что можно автоматизировать."""
        try:
            return self.think_json(
                "Определи задачи в агентстве моделей, которые можно автоматизировать через Telegram-бот или скрипты.\n"
                "Верни JSON:\n"
                '{"automation_opportunities": ['
                '{"task": "название задачи", "current_effort_hrs": 2, "automation_type": "telegram_bot|script|cron|webhook", '
                '"implementation": "краткое описание реализации", "roi": "высокий|средний|низкий"}], '
                '"priority_automation": "самое важное что автоматизировать в первую очередь", '
                '"estimated_savings_hrs_per_week": 10}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[operations/automation_builder] find_automation_opportunities error: %s", e)
            return {}


class CRMSpecialist(FactoryAgent):
    department = "operations"
    role = "crm_specialist"
    name = "crm_specialist"
    system_prompt = """Ты — CRM Specialist агентства моделей Nevesty Models.
Анализируешь данные клиентов: повторные заказы, LTV, сегменты, отток.
Предлагаешь стратегии удержания и роста дохода с существующей базы клиентов.
Всё на русском языке."""

    def analyze_client_data(self, context: dict) -> dict:
        """Анализирует данные клиентов и повторные заказы."""
        try:
            return self.think_json(
                "Проанализируй данные клиентов агентства моделей и предложи CRM-стратегию.\n"
                "Верни JSON:\n"
                '{"client_segments": [{"name": "VIP", "criteria": "...", "strategy": "..."}], '
                '"retention_tactics": ["тактика 1", "тактика 2"], '
                '"upsell_opportunities": [{"segment": "...", "offer": "...", "expected_revenue": "..."}], '
                '"repeat_order_trigger": "триггер для повторного заказа", '
                '"recommended_follow_up": "через сколько дней и как связаться"}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[operations/crm_specialist] analyze_client_data error: %s", e)
            return {}


class TaskScheduler(FactoryAgent):
    department = "operations"
    role = "task_scheduler"
    name = "task_scheduler"
    system_prompt = """Ты — Task Scheduler агентства моделей Nevesty Models.
Составляешь приоритетные списки задач на неделю для команды.
Учитываешь дедлайны мероприятий, сезонность, загрузку.
Всё на русском языке."""

    def schedule_weekly_tasks(self, context: dict) -> dict:
        """Составляет приоритетный план задач на неделю."""
        try:
            return self.think_json(
                "Составь приоритетный список задач для агентства моделей на ближайшую неделю.\n"
                "Верни JSON:\n"
                '{"week_tasks": ['
                '{"day": "Пн", "priority": 1, "task": "...", "assignee": "менеджер|бот|директор", '
                '"duration_hrs": 2, "deadline": "...", "category": "клиент|модель|маркетинг|операции"}], '
                '"week_focus": "главная цель недели", '
                '"blockers": ["что может помешать"], '
                '"success_criteria": "как поймём что неделя прошла успешно"}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[operations/task_scheduler] schedule_weekly_tasks error: %s", e)
            return {}


class SystemOptimizer(FactoryAgent):
    department = "operations"
    role = "system_optimizer"
    name = "system_optimizer"
    system_prompt = """Ты — System Optimizer агентства моделей Nevesty Models.
Анализируешь узкие места в работе: скорость ответа клиентам, конверсию воронки, загрузку системы.
Предлагаешь конкретные улучшения на основе метрик. Всё на русском языке."""

    def analyze_bottlenecks(self, context: dict) -> dict:
        """Находит системные узкие места."""
        try:
            return self.think_json(
                "Найди системные узкие места в работе агентства моделей и предложи решения.\n"
                "Верни JSON:\n"
                '{"bottlenecks": ['
                '{"area": "область", "problem": "описание проблемы", "metric": "как измеряется", '
                '"solution": "конкретное решение", "effort": "малый|средний|большой", '
                '"impact": "высокий|средний|низкий"}], '
                '"system_health": "хорошее|удовлетворительное|критическое", '
                '"top_priority_fix": "самое важное исправление", '
                '"monitoring_metrics": ["метрика для отслеживания"]}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[operations/system_optimizer] analyze_bottlenecks error: %s", e)
            return {}


class OperationsDepartment:
    """Координатор операционного департамента."""

    def __init__(self) -> None:
        self.workflow = WorkflowManager()
        self.automation = AutomationBuilder()
        self.crm = CRMSpecialist()
        self.scheduler = TaskScheduler()
        self.optimizer = SystemOptimizer()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("workflow", "процесс", "оптимиз", "optimize")):
                result_data["workflow"] = self.workflow.optimize_workflow(context)
                roles_used.append("workflow_manager")
        except Exception as e:
            logger.error("[OperationsDept] workflow task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("automat", "автомат", "бот", "скрипт")):
                result_data["automation"] = self.automation.find_automation_opportunities(context)
                roles_used.append("automation_builder")
        except Exception as e:
            logger.error("[OperationsDept] automation task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("crm", "клиент", "повтор", "retention", "client")):
                result_data["crm"] = self.crm.analyze_client_data(context)
                roles_used.append("crm_specialist")
        except Exception as e:
            logger.error("[OperationsDept] crm task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("задач", "план", "schedule", "неделя", "week")):
                result_data["schedule"] = self.scheduler.schedule_weekly_tasks(context)
                roles_used.append("task_scheduler")
        except Exception as e:
            logger.error("[OperationsDept] scheduler task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("систем", "узк", "bottleneck", "performance", "health")) \
                    or not roles_used:
                result_data["optimization"] = self.optimizer.analyze_bottlenecks(context)
                roles_used.append("system_optimizer")
        except Exception as e:
            logger.error("[OperationsDept] optimizer task error: %s", e)

        output = {
            "department": "operations",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[OperationsDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
