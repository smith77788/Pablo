"""🛠️ Tech Department — разработка, API, деплой, QA для Nevesty Models."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from factory.agents.base import FactoryAgent
from factory import db

logger = logging.getLogger(__name__)


class BackendDeveloper(FactoryAgent):
    department = "tech"
    role = "backend_dev"
    name = "backend_developer"
    system_prompt = """Ты — Backend Developer агентства моделей Nevesty Models.
Специализируешься на Node.js, Express и SQLite — технологиях, на которых работает Telegram-бот агентства.
Даёшь конкретные советы по оптимизации серверного кода: запросы к БД, кэширование, обработка ошибок.
Всё на русском языке."""

    def optimize_backend(self, context: dict) -> dict:
        """Советы по оптимизации Node.js/SQLite бэкенда."""
        try:
            return self.think_json(
                "Проанализируй бэкенд Telegram-бота агентства моделей (Node.js + SQLite) и дай рекомендации.\n"
                "Верни JSON:\n"
                '{"performance_issues": [{"area": "...", "problem": "...", "fix": "...", "code_hint": "фрагмент кода"}], '
                '"db_optimizations": ["индекс на поле X", "кэшировать запрос Y"], '
                '"error_handling_gaps": ["где не хватает try/catch"], '
                '"security_concerns": ["уязвимость 1"], '
                '"priority_fixes": ["fix 1", "fix 2"]}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[tech/backend_dev] optimize_backend error: %s", e)
            return {}


class FrontendBuilder(FactoryAgent):
    department = "tech"
    role = "frontend_builder"
    name = "frontend_builder"
    system_prompt = """Ты — Frontend Builder агентства моделей Nevesty Models.
Специализируешься на улучшении UI/UX веб-интерфейса админ-панели агентства.
Знаешь HTML, CSS, vanilla JS. Предлагаешь конкретные улучшения интерфейса.
Всё на русском языке."""

    def suggest_ui_improvements(self, context: dict) -> dict:
        """Рекомендации по улучшению UI/UX."""
        try:
            return self.think_json(
                "Предложи улучшения UI/UX для админ-панели агентства моделей.\n"
                "Верни JSON:\n"
                '{"ux_issues": [{"page": "страница", "problem": "проблема", "fix": "решение", "priority": "высокий|средний|низкий"}], '
                '"mobile_improvements": ["улучшение для мобильных"], '
                '"loading_optimizations": ["оптимизация загрузки"], '
                '"accessibility_fixes": ["ARIA метка", "контраст"], '
                '"new_features": [{"feature": "...", "rationale": "зачем нужно", "effort": "малый|средний|большой"}]}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[tech/frontend_builder] suggest_ui_improvements error: %s", e)
            return {}


class APIEngineer(FactoryAgent):
    department = "tech"
    role = "api_engineer"
    name = "api_engineer"
    system_prompt = """Ты — API Engineer агентства моделей Nevesty Models.
Анализируешь REST API endpoints бота и сайта агентства.
Предлагаешь улучшения структуры, добавление endpoints, версионирование, документацию.
Всё на русском языке."""

    def analyze_api(self, context: dict) -> dict:
        """Анализирует API endpoints и предлагает улучшения."""
        try:
            return self.think_json(
                "Проанализируй API агентства моделей (REST + Telegram Bot API) и предложи улучшения.\n"
                "Верни JSON:\n"
                '{"api_issues": [{"endpoint": "...", "issue": "...", "recommendation": "..."}], '
                '"missing_endpoints": [{"method": "GET|POST|PUT|DELETE", "path": "/api/...", "purpose": "зачем нужен"}], '
                '"versioning_strategy": "как версионировать API", '
                '"rate_limiting": "рекомендации по лимитам", '
                '"documentation_needs": ["что задокументировать"], '
                '"webhook_opportunities": ["где использовать webhook вместо polling"]}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[tech/api_engineer] analyze_api error: %s", e)
            return {}


class DeploymentManager(FactoryAgent):
    department = "tech"
    role = "deployment"
    name = "deployment_manager"
    system_prompt = """Ты — Deployment Manager агентства моделей Nevesty Models.
Отвечаешь за мониторинг здоровья системы, деплой, Docker, резервные копии.
Предлагаешь конкретные шаги по обеспечению надёжности и непрерывности работы.
Всё на русском языке."""

    def check_system_health(self, context: dict) -> dict:
        """Мониторинг здоровья системы и рекомендации по деплою."""
        try:
            return self.think_json(
                "Оцени состояние инфраструктуры агентства моделей и дай рекомендации.\n"
                "Верни JSON:\n"
                '{"health_status": "здоровый|требует внимания|критический", '
                '"monitoring_gaps": ["что не мониторится"], '
                '"deployment_risks": [{"risk": "...", "mitigation": "..."}], '
                '"backup_recommendations": ["рекомендация по бэкапу"], '
                '"uptime_improvements": ["улучшение доступности"], '
                '"next_steps": ["шаг 1", "шаг 2"]}',
                context=context,
                max_tokens=1200,
            ) or {}
        except Exception as e:
            logger.error("[tech/deployment] check_system_health error: %s", e)
            return {}


class QATester(FactoryAgent):
    department = "tech"
    role = "qa_tester"
    name = "qa_tester"
    system_prompt = """Ты — QA Tester агентства моделей Nevesty Models.
Составляешь тест-кейсы для Telegram-бота и веб-интерфейса агентства.
Находишь потенциальные баги до того, как их найдут клиенты.
Всё на русском языке."""

    def generate_test_cases(self, context: dict) -> dict:
        """Генерирует тест-кейсы и находит потенциальные баги."""
        try:
            return self.think_json(
                "Составь тест-кейсы и найди потенциальные баги для Telegram-бота агентства моделей.\n"
                "Верни JSON:\n"
                '{"test_cases": ['
                '{"id": "TC-001", "area": "бот|сайт|API", "scenario": "...", '
                '"steps": ["шаг 1", "шаг 2"], "expected": "...", "priority": "P1|P2|P3"}], '
                '"potential_bugs": ['
                '{"area": "...", "description": "...", "severity": "критический|высокий|средний|низкий", '
                '"reproduction": "как воспроизвести"}], '
                '"edge_cases": ["граничный случай 1", "граничный случай 2"], '
                '"regression_checklist": ["проверка 1 перед каждым деплоем"]}',
                context=context,
                max_tokens=1500,
            ) or {}
        except Exception as e:
            logger.error("[tech/qa_tester] generate_test_cases error: %s", e)
            return {}


class TechDepartment:
    """Координатор технического департамента."""

    def __init__(self):
        self.backend = BackendDeveloper()
        self.frontend = FrontendBuilder()
        self.api = APIEngineer()
        self.deployment = DeploymentManager()
        self.qa = QATester()

    def execute_task(self, task: str, context: dict) -> dict:
        """Диспетчер по ключевым словам задачи."""
        task_lower = task.lower()
        result_data = {}
        roles_used = []

        try:
            if any(kw in task_lower for kw in ("backend", "node", "sqlite", "сервер", "api", "бэкенд")):
                result_data["backend"] = self.backend.optimize_backend(context)
                roles_used.append("backend_dev")
        except Exception as e:
            logger.error("[TechDept] backend task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("frontend", "ui", "ux", "интерфейс", "сайт", "html")):
                result_data["frontend"] = self.frontend.suggest_ui_improvements(context)
                roles_used.append("frontend_builder")
        except Exception as e:
            logger.error("[TechDept] frontend task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("api", "endpoint", "rest", "webhook", "маршрут")):
                result_data["api"] = self.api.analyze_api(context)
                roles_used.append("api_engineer")
        except Exception as e:
            logger.error("[TechDept] api task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("deploy", "деплой", "docker", "мониторинг", "health", "uptime")):
                result_data["deployment"] = self.deployment.check_system_health(context)
                roles_used.append("deployment")
        except Exception as e:
            logger.error("[TechDept] deployment task error: %s", e)

        try:
            if any(kw in task_lower for kw in ("test", "тест", "qa", "баг", "bug", "проверк")) \
                    or not roles_used:
                result_data["qa"] = self.qa.generate_test_cases(context)
                roles_used.append("qa_tester")
        except Exception as e:
            logger.error("[TechDept] qa task error: %s", e)

        output = {
            "department": "tech",
            "task": task,
            "result": result_data,
            "roles_used": roles_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("[TechDept] Задача '%s' выполнена. Ролей задействовано: %d", task, len(roles_used))
        return output
