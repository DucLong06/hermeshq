from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

_PROVIDER_OK_TTL = 900.0
_PROVIDER_OK_CACHE: dict[str, tuple[float, bool]] = {}


async def build_proxy_vault():
    from hermeshq.config import get_settings
    from hermeshq.services.secret_vault import build_vault_from_settings

    return build_vault_from_settings(get_settings())


async def keyed_providers(db: AsyncSession) -> list:
    from sqlalchemy import select

    from hermeshq.models.provider import ProviderDefinition

    result = await db.execute(
        select(ProviderDefinition)
        .where(ProviderDefinition.enabled.is_(True))
        .order_by(ProviderDefinition.sort_order.asc(), ProviderDefinition.name.asc())
    )
    return [d for d in result.scalars().all() if d.api_key_ref and (d.base_url or "").strip()]


async def probe_provider_key(base_url: str, api_key: str, model: str) -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5)) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        return response.status_code < 400
    except httpx.HTTPError:
        return False


async def provider_operational(db: AsyncSession, definition, vault) -> bool:
    import time as _time

    from hermeshq.services.credentials import resolve_secret_value

    cache_key = f"{definition.slug}:{definition.api_key_ref}"
    now = _time.monotonic()
    cached = _PROVIDER_OK_CACHE.get(cache_key)
    if cached and now - cached[0] < _PROVIDER_OK_TTL:
        return cached[1]
    key = await resolve_secret_value(db, vault, definition.api_key_ref)
    model = definition.default_model or next(iter(definition.available_models or []), "")
    ok = bool(key and model) and await probe_provider_key(definition.base_url, key, model)
    _PROVIDER_OK_CACHE[cache_key] = (now, ok)
    return ok


async def model_catalog(db: AsyncSession, agent) -> list[str]:
    import asyncio

    ids: list[str] = []
    if agent.model:
        ids.append(agent.model)
    providers = await keyed_providers(db)
    if providers:
        vault = await build_proxy_vault()
        results = await asyncio.gather(*(provider_operational(db, d, vault) for d in providers))
        for definition, ok in zip(providers, results):
            if not ok:
                continue
            for model in definition.available_models or []:
                if model and model not in ids:
                    ids.append(model)
    return ids
