import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from hermeshq.routers.agents_shared import _apply_agent_runtime_behavior_settings
from hermeshq.services.hermes_runtime import HermesRuntime


def _load(content=None):
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        if content is not None:
            (home / "request_overrides.json").write_text(content, encoding="utf-8")
        return HermesRuntime._load_request_overrides(home)


class RequestOverridesTests(unittest.TestCase):
    def test_missing_file_means_no_overrides(self):
        self.assertEqual(_load(), {})

    def test_valid_object_is_returned(self):
        content = '{"extra_body": {"chat_template_kwargs": {"enable_thinking": false}}}'
        self.assertEqual(_load(content), {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})

    def test_invalid_json_is_ignored(self):
        self.assertEqual(_load("{not json"), {})

    def test_non_object_json_is_ignored(self):
        self.assertEqual(_load("[1, 2, 3]"), {})
        self.assertEqual(_load('"texto"'), {})

    def test_directory_instead_of_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "request_overrides.json").mkdir()
            self.assertEqual(HermesRuntime._load_request_overrides(home), {})


class ThinkingModeTests(unittest.TestCase):
    def test_default_sends_no_override(self):
        self.assertEqual(HermesRuntime._thinking_overrides(None), {})
        self.assertEqual(HermesRuntime._thinking_overrides("inherit"), {})

    def test_off_disables_thinking_for_the_model_server(self):
        self.assertEqual(
            HermesRuntime._thinking_overrides("off"),
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        )

    def test_on_enables_thinking_explicitly(self):
        self.assertEqual(
            HermesRuntime._thinking_overrides("on"),
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
        )

    def test_agent_setting_and_file_are_merged_with_file_taking_precedence(self):
        agent = SimpleNamespace(thinking_mode="off")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "request_overrides.json").write_text(
                '{"extra_body": {"chat_template_kwargs": {"enable_thinking": true, "x": 1}}, "service_tier": "fast"}',
                encoding="utf-8",
            )
            merged = HermesRuntime._build_request_overrides(agent, home)
        self.assertEqual(
            merged,
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": True, "x": 1}}, "service_tier": "fast"},
        )

    def test_agent_without_the_attribute_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(HermesRuntime._build_request_overrides(SimpleNamespace(), Path(tmp)), {})

    def test_merge_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        HermesRuntime._merge_overrides(base, {"a": {"c": 2}})
        self.assertEqual(base, {"a": {"b": 1}})


class ThinkingModeNormalizationTests(unittest.TestCase):
    def _apply(self, value):
        agent = SimpleNamespace(thinking_mode="stale")
        _apply_agent_runtime_behavior_settings(agent, {"thinking_mode": value})
        return agent.thinking_mode

    def test_valid_values_are_normalized(self):
        self.assertEqual(self._apply("off"), "off")
        self.assertEqual(self._apply(" ON "), "on")

    def test_inherit_and_empty_clear_the_setting(self):
        self.assertIsNone(self._apply("inherit"))
        self.assertIsNone(self._apply(""))
        self.assertIsNone(self._apply(None))

    def test_invalid_value_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._apply("maybe")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_untouched_when_not_in_payload(self):
        agent = SimpleNamespace(thinking_mode="off")
        _apply_agent_runtime_behavior_settings(agent, {})
        self.assertEqual(agent.thinking_mode, "off")


class MigrationChainTests(unittest.TestCase):
    def test_alembic_has_a_single_head(self):
        versions = Path(__file__).resolve().parents[1] / "hermeshq" / "alembic" / "versions"
        revisions, downs = set(), set()
        for path in versions.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            revision = re.search(r'^revision(?:: str)? *= *"([^"]+)"', text, re.M)
            down = re.search(r"^down_revision[^=]*=(.+)$", text, re.M)
            if revision:
                revisions.add(revision.group(1))
            if down:
                downs.update(re.findall(r'"([^"]+)"', down.group(1)))
        self.assertEqual(len(revisions - downs), 1, sorted(revisions - downs))


class TestAdaptiveThinking(unittest.TestCase):
    def test_adaptive_starts_with_thinking_on_for_the_planning_call(self):
        self.assertEqual(
            HermesRuntime._thinking_overrides("adaptive"),
            {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}},
        )

    def test_the_agent_setting_accepts_adaptive(self):
        from hermeshq.routers.agents_shared import THINKING_MODE_OPTIONS

        self.assertIn("adaptive", THINKING_MODE_OPTIONS)

    def test_the_runner_turns_thinking_off_only_for_calls_that_follow_a_tool_result(self):
        import importlib.util
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch

        recibidas = []

        class Completions:
            def create(self, *args, **kwargs):
                recibidas.append(kwargs)

        falsos = {"openai": types.ModuleType("openai")}
        for nombre in ("openai.resources", "openai.resources.chat", "openai.resources.chat.completions"):
            falsos[nombre] = types.ModuleType(nombre)
        falsos["openai.resources.chat.completions"].Completions = Completions
        runner_path = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"
        spec = importlib.util.spec_from_file_location("runner_adaptive_under_test", runner_path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with patch.dict(sys.modules, falsos):
            runner._activar_pensamiento_adaptativo()
            llamada = Completions().create
            llamada(messages=[{"role": "user", "content": "hola"}], extra_body={"x": 1})
            llamada(messages=[{"role": "user", "content": "hola"}, {"role": "tool", "content": "r"}])
            llamada(messages=[{"role": "tool", "content": "r"}], extra_body={"chat_template_kwargs": {"a": 1}})
        self.assertEqual(recibidas[0]["extra_body"], {"x": 1})
        self.assertEqual(recibidas[1]["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(recibidas[2]["extra_body"], {"chat_template_kwargs": {"a": 1, "enable_thinking": False}})


class TestPlatformRules(unittest.TestCase):
    def test_every_agent_gets_the_same_rules(self):
        text = HermesRuntime._with_platform_rules("# ROLE\nYou are an assistant.")
        for phrase in ("Keep reasoning brief", "Codes, identifiers", "execute_code", "background"):
            self.assertIn(phrase, text)
        self.assertTrue(text.startswith("# ROLE\nYou are an assistant."))

    def test_the_rules_only_name_tools_that_exist_in_the_platform(self):
        for name in ("hq_trabajo_largo", "hq_leer_varios", "hq_buscar_varios", "hq_skills_ver", "hq_editar_varios"):
            self.assertNotIn(name, HermesRuntime._with_platform_rules(""))


if __name__ == "__main__":
    unittest.main()


class TestScheduledTasksThinking(unittest.TestCase):
    def _task(self, scheduled):
        return SimpleNamespace(metadata_json={"scheduled": True} if scheduled else {"source": "public_chat"})

    def _build(self, mode, task, setting="off"):
        from unittest.mock import patch

        with patch("hermeshq.config.get_settings", return_value=SimpleNamespace(scheduled_tasks_thinking=setting)):
            return HermesRuntime._build_request_overrides(
                SimpleNamespace(thinking_mode=mode), Path("/nonexistent"), task
            )

    def test_scheduled_task_runs_without_thinking_even_if_agent_has_it_on(self):
        out = self._build("on", self._task(True))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)

    def test_scheduled_task_with_inherit_agent_also_turns_it_off(self):
        out = self._build(None, self._task(True))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)

    def test_chat_task_keeps_the_agent_setting(self):
        out = self._build("on", self._task(False))
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], True)

    def test_inherit_setting_leaves_scheduled_tasks_with_the_agent_mode(self):
        out = self._build("on", self._task(True), setting="inherit")
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], True)

    def test_without_a_task_behaviour_is_unchanged(self):
        out = self._build("off", None)
        self.assertEqual(out["extra_body"]["chat_template_kwargs"]["enable_thinking"], False)
