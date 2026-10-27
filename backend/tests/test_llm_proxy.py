from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


class TestLlmRateLimiter:
    def test_rate_limit_blocks_after_limit(self) -> None:
        from hermeshq.routers.enrollment import _LLM_PROXY_RATE_LIMIT, _LLM_RATE_BUCKET, _llm_rate_ok

        _LLM_RATE_BUCKET.clear()
        device_id = "rate-test-device"
        allowed = sum(1 for _ in range(_LLM_PROXY_RATE_LIMIT + 5) if _llm_rate_ok(device_id))
        assert allowed == _LLM_PROXY_RATE_LIMIT
        _LLM_RATE_BUCKET.pop(device_id, None)


class TestLlmModelRouting:
    def _make_db(self, providers: list) -> MagicMock:
        db = MagicMock()
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = providers
        result.scalars.return_value = scalars
        db.execute = AsyncMock(return_value=result)
        return db

    async def test_model_routes_to_keyed_provider(self) -> None:
        from types import SimpleNamespace

        from hermeshq.routers.enrollment import _resolve_llm_target

        zai = SimpleNamespace(
            slug="zai",
            enabled=True,
            api_key_ref="z_ai_coding_plan",
            base_url="https://api.z.ai/api/coding/paas/v4/",
            available_models=["glm-5.3-flash", "glm-5-turbo"],
            sort_order=20,
            name="Z.AI Coding Plan",
        )
        agent = SimpleNamespace(
            model="qwen3.8-27b",
            provider="openai-codex",
            base_url="https://puget.sixmanager.io/v1",
            api_key_ref="puget-key",
        )
        db = self._make_db([zai])
        db.get = AsyncMock(side_effect=lambda m, k: zai if k == "zai" else None)

        import hermeshq.routers.enrollment as enrollment_router

        original = enrollment_router._llm_proxy_vault

        async def fake_vault():
            return "vault"

        enrollment_router._llm_proxy_vault = fake_vault
        try:
            import hermeshq.services.credentials as credentials

            original_resolve = credentials.resolve_secret_value

            async def fake_resolve(session, vault, name):
                return {"z_ai_coding_plan": "zai-key", "puget-key": "puget-key-val"}.get(name)

            credentials.resolve_secret_value = fake_resolve
            try:
                key, base = await _resolve_llm_target(db, agent, "glm-5.3-flash")
                assert key == "zai-key"
                assert base == "https://api.z.ai/api/coding/paas/v4"
                key, base = await _resolve_llm_target(db, agent, "qwen3.8-27b")
                assert key == "puget-key-val"
                assert base == "https://puget.sixmanager.io/v1"
                key, base = await _resolve_llm_target(db, agent, "modelo-desconocido")
                assert key == "puget-key-val"
                assert base == "https://puget.sixmanager.io/v1"
            finally:
                credentials.resolve_secret_value = original_resolve
        finally:
            enrollment_router._llm_proxy_vault = original

    async def test_model_catalog_agent_default_first(self) -> None:
        from types import SimpleNamespace

        from hermeshq.routers.enrollment import _llm_model_catalog

        zai = SimpleNamespace(
            enabled=True,
            api_key_ref="z_ai_coding_plan",
            base_url="https://api.z.ai/api/coding/paas/v4",
            available_models=["glm-5-turbo", "glm-5.3-flash"],
            sort_order=20,
            name="Z.AI Coding Plan",
        )
        no_key = SimpleNamespace(
            enabled=True,
            api_key_ref=None,
            base_url="https://openrouter.ai/api/v1",
            available_models=["anthropic/claude-sonnet-4"],
            sort_order=30,
            name="OpenRouter API",
        )
        agent = SimpleNamespace(model="qwen3.8-27b")
        ids = await _llm_model_catalog(self._make_db([zai, no_key]), agent)
        assert ids[0] == "qwen3.8-27b"
        assert "glm-5.3-flash" in ids
        assert "glm-5-turbo" in ids
        assert all("claude" not in m for m in ids), "providers without key must not be exposed"


class TestBundleLlmProxy:
    async def test_bundle_routes_llm_via_proxy(self) -> None:
        from hermeshq.services.enrollment import EnrollmentService

        service = EnrollmentService.__new__(EnrollmentService)
        service._vault = None
        service._enforcer = MagicMock()
        merged_policy = SimpleNamespace(name="P", tool_rules={}, path_rules={}, command_rules={}, network_rules={})
        service._enforcer.get_policies = AsyncMock(return_value=[merged_policy])
        service._resolve_agent_api_key = AsyncMock(return_value="sk-real-provider-key")
        service._guard_plugin_files = MagicMock(return_value={"__init__.py": "x"})

        device = SimpleNamespace(id="d1", agent_id="a1", token_version=3, guard_fail_mode="fail-closed")
        agent = SimpleNamespace(
            id="a1",
            name="Operador",
            slug="operador",
            friendly_name="Operador",
            system_prompt="",
            soul_md="",
            model="qwen3.8-27b",
            provider="puget",
            base_url="https://puget.sixmanager.io/v1",
            hermes_version=None,
            api_key_ref="puget-key",
            skills=[],
            is_archived=False,
        )

        async def session_ctx():
            return self

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, model, key):
                return agent if model.__name__ == "Agent" else None

        service.session_factory = FakeSession
        built = await service.build_bundle(device)
        payload = built["bundle"]
        assert payload["llm_proxy"]["enabled"] is True
        assert "/api/enrollment/llm/v1" in payload["llm_proxy"]["base_url"]
        assert payload["agent"]["api_key"] is None, "real provider key must NOT ship to the device"
        assert payload["agent"]["base_url"] == payload["llm_proxy"]["base_url"]
        assert payload["agent"]["custom_provider"]["base_url"] == payload["llm_proxy"]["base_url"]
        assert payload["guard"]["env"]["HERMESHQ_AGENT_TOKEN"]
        etag = built["etag"]
        assert payload["llm_proxy"]["api_key"]
        second = await EnrollmentService.build_bundle(service, device)
        assert second["etag"] == etag
