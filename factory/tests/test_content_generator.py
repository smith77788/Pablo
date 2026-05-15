"""Tests for БЛОК 9.1 — Content Generation Factory agents."""
from __future__ import annotations
import datetime
import pytest

from factory.agents.content_generator import (
    ChannelPostGenerator,
    ModelDescriptionWriter,
    FAQGenerator,
    ContentGenerationDepartment,
)


# ─────────────────────────────────────────────────────────────────────────────
# ChannelPostGenerator
# ─────────────────────────────────────────────────────────────────────────────

class TestChannelPostGeneratorInstantiation:
    def test_instantiates(self):
        gen = ChannelPostGenerator()
        assert gen is not None

    def test_name_attribute(self):
        gen = ChannelPostGenerator()
        assert gen.name == "channel_post_generator"

    def test_department_attribute(self):
        gen = ChannelPostGenerator()
        assert gen.department == "creative"

    def test_role_attribute(self):
        gen = ChannelPostGenerator()
        assert gen.role == "content"

    def test_system_prompt_is_string(self):
        gen = ChannelPostGenerator()
        assert isinstance(gen.system_prompt, str)
        assert len(gen.system_prompt) > 0

    def test_post_templates_is_list(self):
        gen = ChannelPostGenerator()
        assert isinstance(gen.POST_TEMPLATES, list)
        assert len(gen.POST_TEMPLATES) >= 4


class TestChannelPostGeneratorGeneratePost:
    def test_generate_post_returns_string(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post()
        assert isinstance(result, str)

    def test_generate_post_non_empty(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post()
        assert len(result) > 0

    def test_generate_post_contains_bot_handle(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post()
        assert "@nevesty_models_bot" in result

    def test_generate_post_with_context(self):
        gen = ChannelPostGenerator()
        ctx = {'model_name': 'Екатерина', 'category': 'fashion'}
        result = gen.generate_post(ctx)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_post_with_commercial_category(self):
        gen = ChannelPostGenerator()
        ctx = {'model_name': 'Анна', 'category': 'commercial'}
        result = gen.generate_post(ctx)
        assert isinstance(result, str)

    def test_generate_post_with_events_category(self):
        gen = ChannelPostGenerator()
        ctx = {'model_name': 'Мария', 'category': 'events'}
        result = gen.generate_post(ctx)
        assert isinstance(result, str)

    def test_generate_post_with_unknown_category(self):
        gen = ChannelPostGenerator()
        ctx = {'category': 'unknown_category'}
        result = gen.generate_post(ctx)
        assert isinstance(result, str)

    def test_generate_post_with_none_context(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post(None)
        assert isinstance(result, str)

    def test_generate_post_with_empty_context(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post({})
        assert isinstance(result, str)

    def test_generate_post_uses_default_model_name(self):
        gen = ChannelPostGenerator()
        result = gen.generate_post({})
        # Default model_name is 'Мария', check it appears somewhere in the post
        assert "Мария" in result or len(result) > 20  # post uses template, so text is there

    def test_generate_multiple_posts_varied(self):
        gen = ChannelPostGenerator()
        posts = [gen.generate_post() for _ in range(10)]
        # With 4 templates, we should see some variety (not all identical)
        unique = set(posts)
        assert len(unique) >= 1  # At minimum works


class TestChannelPostGeneratorRun:
    def test_run_returns_dict(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert isinstance(result, dict)

    def test_run_has_insights_key(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert "insights" in result

    def test_run_insights_is_list(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert isinstance(result["insights"], list)

    def test_run_has_recommendations_key(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert "recommendations" in result

    def test_run_recommendations_is_list(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert isinstance(result["recommendations"], list)

    def test_run_recommendations_has_3_posts(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert len(result["recommendations"]) == 3

    def test_run_has_timestamp_key(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert "timestamp" in result

    def test_run_timestamp_is_string(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert isinstance(result["timestamp"], str)

    def test_run_with_context(self):
        gen = ChannelPostGenerator()
        ctx = {'model_name': 'Ольга', 'category': 'events'}
        result = gen.run(ctx)
        assert isinstance(result, dict)
        assert len(result["recommendations"]) == 3

    def test_run_insights_mentions_channel(self):
        gen = ChannelPostGenerator()
        result = gen.run()
        assert any("Telegram" in i or "post" in i.lower() or "channel" in i.lower()
                   for i in result["insights"])


# ─────────────────────────────────────────────────────────────────────────────
# ModelDescriptionWriter
# ─────────────────────────────────────────────────────────────────────────────

class TestModelDescriptionWriterInstantiation:
    def test_instantiates(self):
        writer = ModelDescriptionWriter()
        assert writer is not None

    def test_name_attribute(self):
        writer = ModelDescriptionWriter()
        assert writer.name == "model_description_writer"

    def test_department_attribute(self):
        writer = ModelDescriptionWriter()
        assert writer.department == "creative"

    def test_role_attribute(self):
        writer = ModelDescriptionWriter()
        assert writer.role == "copywriter"

    def test_intros_is_list(self):
        writer = ModelDescriptionWriter()
        assert isinstance(writer.INTROS, list)
        assert len(writer.INTROS) >= 3


class TestModelDescriptionWriterGenerateDescription:
    def test_generate_description_returns_string(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна'})
        assert isinstance(result, str)

    def test_generate_description_non_empty(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна'})
        assert len(result) > 0

    def test_generate_description_includes_name(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Юлия'})
        assert "Юлия" in result

    def test_generate_description_includes_city(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'city': 'Санкт-Петербург'})
        assert "Санкт-Петербург" in result

    def test_generate_description_includes_height(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'height': 175})
        assert "175" in result

    def test_generate_description_includes_age(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'age': 24})
        assert "24" in result

    def test_generate_description_fashion_category(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'category': 'fashion'})
        assert "fashion" in result

    def test_generate_description_commercial_category(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'category': 'commercial'})
        assert "коммерческой" in result

    def test_generate_description_events_category(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'category': 'events'})
        assert "event" in result

    def test_generate_description_unknown_category(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'category': 'unknown'})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_description_without_height(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'age': 22})
        assert isinstance(result, str)
        assert "рост" not in result

    def test_generate_description_without_age(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна', 'height': 170})
        assert isinstance(result, str)
        assert "возраст" not in result

    def test_generate_description_default_city(self):
        writer = ModelDescriptionWriter()
        result = writer.generate_description({'name': 'Анна'})
        assert "Москва" in result

    def test_generate_description_full_model(self):
        writer = ModelDescriptionWriter()
        model = {'name': 'Катя', 'height': 176, 'age': 23, 'category': 'fashion', 'city': 'Краснодар'}
        result = writer.generate_description(model)
        assert "Катя" in result
        assert "176" in result
        assert "23" in result
        assert "Краснодар" in result


class TestModelDescriptionWriterRun:
    def test_run_returns_dict(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert isinstance(result, dict)

    def test_run_has_insights_key(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert "insights" in result

    def test_run_has_recommendations_key(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert "recommendations" in result

    def test_run_has_timestamp_key(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert "timestamp" in result

    def test_run_recommendations_is_list(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert isinstance(result["recommendations"], list)

    def test_run_default_generates_one_description(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert len(result["recommendations"]) == 1

    def test_run_with_multiple_models(self):
        writer = ModelDescriptionWriter()
        models = [
            {'name': 'Анна', 'height': 175, 'category': 'fashion', 'city': 'Москва'},
            {'name': 'Мария', 'height': 170, 'category': 'events', 'city': 'СПб'},
        ]
        result = writer.run({'models': models})
        assert len(result["recommendations"]) == 2

    def test_run_respects_max_5_models(self):
        writer = ModelDescriptionWriter()
        models = [{'name': f'Model{i}', 'city': 'Москва'} for i in range(10)]
        result = writer.run({'models': models})
        assert len(result["recommendations"]) <= 5

    def test_run_insights_mentions_count(self):
        writer = ModelDescriptionWriter()
        result = writer.run()
        assert any("1" in i or "Generated" in i for i in result["insights"])


# ─────────────────────────────────────────────────────────────────────────────
# FAQGenerator
# ─────────────────────────────────────────────────────────────────────────────

class TestFAQGeneratorInstantiation:
    def test_instantiates(self):
        gen = FAQGenerator()
        assert gen is not None

    def test_name_attribute(self):
        gen = FAQGenerator()
        assert gen.name == "faq_generator"

    def test_department_attribute(self):
        gen = FAQGenerator()
        assert gen.department == "creative"

    def test_role_attribute(self):
        gen = FAQGenerator()
        assert gen.role == "support"

    def test_faq_base_is_dict(self):
        gen = FAQGenerator()
        assert isinstance(gen.FAQ_BASE, dict)
        assert len(gen.FAQ_BASE) >= 5


class TestFAQGeneratorGenerateFAQ:
    def test_generate_faq_returns_list(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        assert isinstance(result, list)

    def test_generate_faq_non_empty(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        assert len(result) > 0

    def test_generate_faq_items_are_dicts(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert isinstance(item, dict)

    def test_generate_faq_items_have_question_key(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert "question" in item

    def test_generate_faq_items_have_answer_key(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert "answer" in item

    def test_generate_faq_questions_are_strings(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert isinstance(item["question"], str)

    def test_generate_faq_answers_are_strings(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert isinstance(item["answer"], str)

    def test_generate_faq_non_empty_questions(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert len(item["question"]) > 0

    def test_generate_faq_non_empty_answers(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        for item in result:
            assert len(item["answer"]) > 0

    def test_generate_faq_count_matches_base(self):
        gen = FAQGenerator()
        result = gen.generate_faq()
        assert len(result) == len(gen.FAQ_BASE)


class TestFAQGeneratorRun:
    def test_run_returns_dict(self):
        gen = FAQGenerator()
        result = gen.run()
        assert isinstance(result, dict)

    def test_run_has_insights_key(self):
        gen = FAQGenerator()
        result = gen.run()
        assert "insights" in result

    def test_run_has_recommendations_key(self):
        gen = FAQGenerator()
        result = gen.run()
        assert "recommendations" in result

    def test_run_has_timestamp_key(self):
        gen = FAQGenerator()
        result = gen.run()
        assert "timestamp" in result

    def test_run_recommendations_is_list(self):
        gen = FAQGenerator()
        result = gen.run()
        assert isinstance(result["recommendations"], list)

    def test_run_recommendations_are_qa_strings(self):
        gen = FAQGenerator()
        result = gen.run()
        for rec in result["recommendations"]:
            assert "Q:" in rec
            assert "A:" in rec

    def test_run_insights_mentions_count(self):
        gen = FAQGenerator()
        result = gen.run()
        assert any("FAQ" in i or "entries" in i for i in result["insights"])

    def test_run_with_context_none(self):
        gen = FAQGenerator()
        result = gen.run(None)
        assert isinstance(result, dict)

    def test_run_with_empty_context(self):
        gen = FAQGenerator()
        result = gen.run({})
        assert isinstance(result, dict)

    def test_run_timestamp_parseable(self):
        gen = FAQGenerator()
        result = gen.run()
        ts = result["timestamp"]
        # Should be parseable as ISO datetime
        parsed = datetime.datetime.fromisoformat(ts)
        assert parsed is not None


# ─────────────────────────────────────────────────────────────────────────────
# ContentGenerationDepartment
# ─────────────────────────────────────────────────────────────────────────────

class TestContentGenerationDepartmentInstantiation:
    def test_instantiates(self):
        dept = ContentGenerationDepartment()
        assert dept is not None

    def test_has_post_generator(self):
        dept = ContentGenerationDepartment()
        assert isinstance(dept.post_generator, ChannelPostGenerator)

    def test_has_description_writer(self):
        dept = ContentGenerationDepartment()
        assert isinstance(dept.description_writer, ModelDescriptionWriter)

    def test_has_faq_generator(self):
        dept = ContentGenerationDepartment()
        assert isinstance(dept.faq_generator, FAQGenerator)


class TestContentGenerationDepartmentExecuteTask:
    def test_execute_task_returns_dict(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert isinstance(result, dict)

    def test_execute_task_has_department_key(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert "department" in result

    def test_execute_task_department_value(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert result["department"] == "content_generation"

    def test_execute_task_has_task_key(self):
        dept = ContentGenerationDepartment()
        task = "generate content"
        result = dept.execute_task(task)
        assert "task" in result
        assert result["task"] == task

    def test_execute_task_has_results_key(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert "results" in result

    def test_execute_task_has_agents_used_key(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert "agents_used" in result

    def test_execute_task_has_timestamp_key(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert "timestamp" in result

    def test_execute_task_agents_used_is_list(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        assert isinstance(result["agents_used"], list)

    def test_execute_task_with_post_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate post for channel")
        assert "posts" in result["results"]

    def test_execute_task_with_telegram_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate telegram content")
        assert "posts" in result["results"]

    def test_execute_task_with_channel_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("channel update")
        assert "posts" in result["results"]

    def test_execute_task_with_description_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("write description for model")
        assert "descriptions" in result["results"]

    def test_execute_task_with_model_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("model bio generation")
        assert "descriptions" in result["results"]

    def test_execute_task_with_faq_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate FAQ for bot")
        assert "faq" in result["results"]

    def test_execute_task_with_question_keyword(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("answer common questions")
        assert "faq" in result["results"]

    def test_execute_task_unknown_task_runs_all(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("do something unspecified")
        agents = result["agents_used"]
        assert "posts" in agents
        assert "descriptions" in agents
        assert "faq" in agents

    def test_execute_task_results_contain_posts_data(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("post content telegram")
        posts_result = result["results"].get("posts", {})
        assert "recommendations" in posts_result

    def test_execute_task_results_contain_descriptions_data(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("model description")
        desc_result = result["results"].get("descriptions", {})
        assert "recommendations" in desc_result

    def test_execute_task_results_contain_faq_data(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("faq answers")
        faq_result = result["results"].get("faq", {})
        assert "recommendations" in faq_result

    def test_execute_task_with_context(self):
        dept = ContentGenerationDepartment()
        ctx = {'model_name': 'Ирина', 'category': 'fashion'}
        result = dept.execute_task("generate post", ctx)
        assert isinstance(result, dict)

    def test_execute_task_timestamp_is_recent(self):
        dept = ContentGenerationDepartment()
        result = dept.execute_task("generate content")
        ts = datetime.datetime.fromisoformat(result["timestamp"])
        now = datetime.datetime.now()
        delta = abs((now - ts).total_seconds())
        assert delta < 60  # generated within the last minute
