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
