"""Mem0 memory layer — persists conversation context across sessions.

Uses the Foundry project endpoint (via service principal) for both the
LLM (memory extraction) and the embedder.
Azure AI Search is used as the persistent vector store.

Required env vars (loaded from custom_ux/.env):
    FOUNDRY_PROJECT_ENDPOINT       — Foundry project endpoint (base URL derived from this)
    AZURE_TENANT_ID                — SP tenant
    APP_CLIENT_ID                  — SP client ID
    APP_CLIENT_SECRET              — SP client secret
    AZURE_OPENAI_DEPLOYMENT_NAME   — LLM deployment (e.g. gpt-5.4)
    AOAI_EMBEDDING_DEPLOYMENT_NAME — Embedding deployment (e.g. text-embedding-3-small)
    AOAI_EMBEDDING_API_VERSION     — Embedding API version (e.g. 2024-06-01)
    AZURE_AI_SEARCH_SERVICE_NAME   — Azure AI Search service name
    AZURE_AI_SEARCH_API_KEY        — Azure AI Search admin API key
"""

import logging
import os
from urllib.parse import urlparse

from azure.identity import ClientSecretCredential, get_bearer_token_provider
from mem0 import Memory

logger = logging.getLogger(__name__)


def _get_token_provider():
    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["APP_CLIENT_ID"],
        client_secret=os.environ["APP_CLIENT_SECRET"],
    )
    return get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")


def _foundry_base_endpoint() -> str:
    """Derive the AI Services base endpoint from the Foundry project URL."""
    parsed = urlparse(os.environ["FOUNDRY_PROJECT_ENDPOINT"])
    return f"{parsed.scheme}://{parsed.hostname}"


def _build_config() -> dict:
    endpoint = _foundry_base_endpoint()
    token_provider = _get_token_provider()
    llm_deployment = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
    embed_deployment = os.environ["AOAI_EMBEDDING_DEPLOYMENT_NAME"]
    embed_api_version = os.environ.get("AOAI_EMBEDDING_API_VERSION", "2024-06-01")
    search_service_name = os.environ["AZURE_AI_SEARCH_SERVICE_NAME"]
    search_api_key = os.environ["AZURE_AI_SEARCH_API_KEY"]

    return {
        "llm": {
            "provider": "azure_openai",
            "config": {
                "model": llm_deployment,
                "temperature": 0.1,
                "azure_kwargs": {
                    "azure_deployment": llm_deployment,
                    "azure_endpoint": endpoint,
                    "azure_ad_token_provider": token_provider,
                    "api_version": "2024-06-01",
                },
            },
        },
        "embedder": {
            "provider": "azure_openai",
            "config": {
                "model": embed_deployment,
                "azure_kwargs": {
                    "azure_deployment": embed_deployment,
                    "azure_endpoint": endpoint,
                    "azure_ad_token_provider": token_provider,
                    "api_version": embed_api_version,
                },
            },
        },
        "vector_store": {
            "provider": "azure_ai_search",
            "config": {
                "service_name": search_service_name,
                "api_key": search_api_key,
                "collection_name": "fabric_agent_memories",
                "embedding_model_dims": 1536,
            },
        },
    }


def create_memory() -> Memory:
    config = _build_config()
    logger.info("Initializing Mem0 with Azure OpenAI (LLM=%s, Embed=%s) + Azure AI Search",
                config["llm"]["config"]["model"],
                config["embedder"]["config"]["model"])
    return Memory.from_config(config)
