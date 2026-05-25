"""Railway GraphQL API client for environment variable management."""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_GQL = "https://backboard.railway.app/graphql/v2"


def _creds() -> tuple[str, str, str, str]:
    return (
        os.getenv("RAILWAY_TOKEN", ""),
        os.getenv("RAILWAY_PROJECT_ID", ""),
        os.getenv("RAILWAY_SERVICE_ID", ""),
        os.getenv("RAILWAY_ENVIRONMENT_ID", ""),
    )


async def _gql(http: aiohttp.ClientSession, query: str, variables: dict) -> dict:
    token, *_ = _creds()
    async with http.post(
        _GQL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        data = await r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"][0].get("message", str(data["errors"])))
    return data.get("data", {})


async def list_variables(http: aiohttp.ClientSession) -> dict[str, str]:
    """Return dict of {KEY: value} for the configured service/environment."""
    token, project_id, service_id, env_id = _creds()
    if not all([token, project_id, service_id, env_id]):
        raise RuntimeError("RAILWAY_TOKEN / RAILWAY_PROJECT_ID / RAILWAY_SERVICE_ID / RAILWAY_ENVIRONMENT_ID не настроены")

    data = await _gql(http, """
        query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
            variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
    """, {"projectId": project_id, "environmentId": env_id, "serviceId": service_id})

    return dict(data.get("variables", {}))


async def set_variable(http: aiohttp.ClientSession, key: str, value: str) -> None:
    """Create or update a single environment variable."""
    token, project_id, service_id, env_id = _creds()
    if not all([token, project_id, service_id, env_id]):
        raise RuntimeError("Railway API не настроен")

    await _gql(http, """
        mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
            variableCollectionUpsert(input: $input)
        }
    """, {"input": {
        "projectId": project_id,
        "environmentId": env_id,
        "serviceId": service_id,
        "variables": {key: value},
    }})


async def delete_variable(http: aiohttp.ClientSession, key: str) -> None:
    """Delete an environment variable."""
    token, project_id, service_id, env_id = _creds()
    if not all([token, project_id, service_id, env_id]):
        raise RuntimeError("Railway API не настроен")

    await _gql(http, """
        mutation VariableDelete($input: VariableDeleteInput!) {
            variableDelete(input: $input)
        }
    """, {"input": {
        "projectId": project_id,
        "environmentId": env_id,
        "serviceId": service_id,
        "name": key,
    }})


def is_configured() -> bool:
    token, project_id, service_id, env_id = _creds()
    return all([token, project_id, service_id, env_id])
