"""Railway GraphQL API client for environment variable management.

Required env vars (minimum):
  RAILWAY_TOKEN      — API token from railway.com/account/tokens
  RAILWAY_PROJECT_ID — UUID from project URL

Optional (auto-discovered if missing):
  RAILWAY_SERVICE_ID      — first service in the project
  RAILWAY_ENVIRONMENT_ID  — first environment (usually 'production')
"""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

_GQL = "https://backboard.railway.app/graphql/v2"

# In-process cache so we only auto-discover once per restart
_discovered: dict[str, str] = {}


def _token() -> str:
    return os.getenv("RAILWAY_TOKEN", "")


def _project_id() -> str:
    return os.getenv("RAILWAY_PROJECT_ID", "")


async def _gql(http: aiohttp.ClientSession, query: str, variables: dict) -> dict:
    token = _token()
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


async def _resolve_ids(http: aiohttp.ClientSession) -> tuple[str, str]:
    """Return (service_id, environment_id), auto-discovering if not set."""
    service_id = os.getenv("RAILWAY_SERVICE_ID") or _discovered.get("service_id", "")
    env_id = os.getenv("RAILWAY_ENVIRONMENT_ID") or _discovered.get("env_id", "")

    if service_id and env_id:
        return service_id, env_id

    project_id = _project_id()
    if not project_id:
        raise RuntimeError("RAILWAY_PROJECT_ID не задан")

    data = await _gql(http, """
        query Project($id: String!) {
            project(id: $id) {
                services { edges { node { id name } } }
                environments { edges { node { id name } } }
            }
        }
    """, {"id": project_id})

    project = data.get("project", {})
    services = [e["node"] for e in project.get("services", {}).get("edges", [])]
    envs = [e["node"] for e in project.get("environments", {}).get("edges", [])]

    if not services:
        raise RuntimeError("В проекте нет сервисов")
    if not envs:
        raise RuntimeError("В проекте нет окружений (environments)")

    # Prefer first service; prefer 'production' environment
    svc = services[0]
    env = next((e for e in envs if "prod" in e["name"].lower()), envs[0])

    _discovered["service_id"] = svc["id"]
    _discovered["env_id"] = env["id"]
    log.info("Railway auto-discovered: service=%s (%s), env=%s (%s)",
             svc["id"], svc["name"], env["id"], env["name"])

    return svc["id"], env["id"]


async def list_variables(http: aiohttp.ClientSession) -> dict[str, str]:
    """Return {KEY: value} for the project's first service/environment."""
    if not _token() or not _project_id():
        raise RuntimeError("Задайте RAILWAY_TOKEN и RAILWAY_PROJECT_ID")

    service_id, env_id = await _resolve_ids(http)
    data = await _gql(http, """
        query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
            variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
    """, {"projectId": _project_id(), "environmentId": env_id, "serviceId": service_id})

    return dict(data.get("variables", {}))


async def set_variable(http: aiohttp.ClientSession, key: str, value: str) -> None:
    """Create or update a single environment variable."""
    if not _token() or not _project_id():
        raise RuntimeError("Задайте RAILWAY_TOKEN и RAILWAY_PROJECT_ID")

    service_id, env_id = await _resolve_ids(http)
    await _gql(http, """
        mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
            variableCollectionUpsert(input: $input)
        }
    """, {"input": {
        "projectId": _project_id(),
        "environmentId": env_id,
        "serviceId": service_id,
        "variables": {key: value},
    }})


async def delete_variable(http: aiohttp.ClientSession, key: str) -> None:
    """Delete an environment variable."""
    if not _token() or not _project_id():
        raise RuntimeError("Задайте RAILWAY_TOKEN и RAILWAY_PROJECT_ID")

    service_id, env_id = await _resolve_ids(http)
    await _gql(http, """
        mutation VariableDelete($input: VariableDeleteInput!) {
            variableDelete(input: $input)
        }
    """, {"input": {
        "projectId": _project_id(),
        "environmentId": env_id,
        "serviceId": service_id,
        "name": key,
    }})


def is_configured() -> bool:
    return bool(_token() and _project_id())


async def get_deployment(http: aiohttp.ClientSession, deployment_id: str) -> dict | None:
    """Get deployment details from Railway GraphQL API."""
    if not is_configured():
        return None
    data = await _gql(http, """
        query Deployment($id: String!) {
            deployment(id: $id) {
                id
                status
                createdAt
                commit { sha message branch }
                creator { name avatar }
                service { name id }
                environment { name id }
                project { name id }
            }
        }
    """, {"id": deployment_id})
    return data.get("deployment")


async def get_recent_deployments(http: aiohttp.ClientSession, limit: int = 5) -> list[dict]:
    """Get recent deployments for the project's environment."""
    if not is_configured():
        return []
    service_id, env_id = await _resolve_ids(http)
    data = await _gql(http, """
        query Deployments($projectId: String!, $environmentId: String!, $serviceId: String!, $first: Int!) {
            deployments(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId, first: $first) {
                edges {
                    node {
                        id
                        status
                        createdAt
                        commit { sha message branch }
                        creator { name avatar }
                        service { name }
                        environment { name }
                    }
                }
            }
        }
    """, {"projectId": _project_id(), "environmentId": env_id, "serviceId": service_id, "first": limit})
    return [e["node"] for e in data.get("deployments", {}).get("edges", [])]
