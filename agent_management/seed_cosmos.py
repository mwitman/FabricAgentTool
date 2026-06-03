"""Seed Cosmos DB with all 5 agent projects.

1. AdventureWorks Orchestrator (orchestrator mode, 3 subagents)
2. Bakehouse Agent (update existing - fix type)
3. All-up Fabric Agent (standalone)
4. Products MLV Agent (standalone)
5. Sales Order MLV Agent (standalone)

Usage:
    python seed_cosmos.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from azure.identity import ClientSecretCredential
from azure.cosmos import CosmosClient

# ---------------------------------------------------------------------------
# Read prompt files
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent

def read_prompt(relative_path: str) -> str:
    p = REPO_ROOT / relative_path
    if not p.exists():
        print(f"WARNING: prompt file not found: {p}")
        return ""
    return p.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Prompt contents
# ---------------------------------------------------------------------------
orchestrator_prompt = read_prompt("agent_api/orchestrator_agent/prompts/orchestrator_agent.md")
sales_prompt = read_prompt("agent_api/orchestrator_agent/prompts/sales_agent.md")
customer_prompt = read_prompt("agent_api/orchestrator_agent/prompts/customer_agent.md")
product_prompt = read_prompt("agent_api/orchestrator_agent/prompts/product_agent.md")
all_up_fabric_prompt = read_prompt("agent_api/all_up_fabric_agent/prompts/all_up_fabric_agent.md")
products_mlv_prompt = read_prompt("agent_api/products_mlv_agent/prompts/products_mlv_agent.md")
sales_order_mlv_prompt = read_prompt("agent_api/sales_order_mlv_agent/prompts/sales_order_mlv_agent.md")

now = datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Project definitions
# ---------------------------------------------------------------------------

# Stable IDs so re-running is idempotent (upsert)
ORCHESTRATOR_ID = "ae2c44be-df0c-4c6f-a68d-0805c080eda6"
BAKEHOUSE_ID = "8498bd90-6fc1-4242-b361-3cfcd0b280ed"
ALL_UP_FABRIC_ID = "c71f2a10-4e89-4b3a-9d6e-1a2b3c4d5e6f"
PRODUCTS_MLV_ID = "d82g3b21-5f9a-5c4b-ae7f-2b3c4d5e6f70"
SALES_ORDER_MLV_ID = "e93h4c32-6g0b-6d5c-bf80-3c4d5e6f7081"


def _empty_semantic_model():
    return {"workspace_id": "", "workspace_name": "", "semantic_model_id": "", "semantic_model_name": ""}


def _empty_model_config():
    return {"deployment_name": "", "model_display_name": ""}


def _empty_standalone():
    return {
        "name": "Standalone Fabric Agent",
        "description": "",
        "semantic_model": _empty_semantic_model(),
        "model_config": _empty_model_config(),
        "prompt": "",
    }


# 1) AdventureWorks Orchestrator
adventureworks_orchestrator = {
    "id": ORCHESTRATOR_ID,
    "type": "agent_project",
    "name": "AdventureWorks Orchestrator",
    "description": "Orchestrates Sales, Customer, and Product sub-agents that query Fabric GraphQL APIs for AdventureWorks data.",
    "deployment_mode": "orchestrator",
    "orchestrator": {
        "name": "GraphQL Agents Orchestrator",
        "description": "Orchestrates three sub-agents for Sales, Customer, and Product data via Fabric GraphQL.",
        "prompt": orchestrator_prompt,
        "model_config": _empty_model_config(),
        "subagents": [
            {
                "id": "sales-agent",
                "name": "Sales Agent",
                "description": "Queries sales data (orders, order details, totals, status) via Fabric GraphQL.",
                "semantic_model": _empty_semantic_model(),
                "model_config": _empty_model_config(),
                "prompt": sales_prompt,
                "guardrails": [],
            },
            {
                "id": "customer-agent",
                "name": "Customer Agent",
                "description": "Queries customer data (identity, addresses) via Fabric GraphQL.",
                "semantic_model": _empty_semantic_model(),
                "model_config": _empty_model_config(),
                "prompt": customer_prompt,
                "guardrails": [],
            },
            {
                "id": "product-agent",
                "name": "Product Agent",
                "description": "Queries product data (products, categories, models, descriptions) via Fabric GraphQL.",
                "semantic_model": _empty_semantic_model(),
                "model_config": _empty_model_config(),
                "prompt": product_prompt,
                "guardrails": [],
            },
        ],
    },
    "standalone_agent": _empty_standalone(),
    "deployment": {},
    "created_at": now,
    "updated_at": now,
    "projectid": ORCHESTRATOR_ID,
}


# 2) Bakehouse Agent — will be merged with existing Cosmos doc
bakehouse_update = {
    "type": "agent_project",  # fix from stitch_project
    "updated_at": now,
}


# 3) All-up Fabric Agent (standalone)
all_up_fabric = {
    "id": ALL_UP_FABRIC_ID,
    "type": "agent_project",
    "name": "All-up Fabric Agent",
    "description": "Discovers Fabric semantic models and executes DAX queries to answer business questions across any connected dataset.",
    "deployment_mode": "standalone",
    "orchestrator": {
        "name": "Fabric Orchestrator",
        "description": "",
        "prompt": "",
        "model_config": _empty_model_config(),
        "subagents": [],
    },
    "standalone_agent": {
        "name": "All-up Fabric Agent",
        "description": "Discovers the best Fabric semantic model for a question using Fabric Core MCP-style discovery, then executes DAX against the selected semantic model.",
        "semantic_model": _empty_semantic_model(),
        "model_config": _empty_model_config(),
        "prompt": all_up_fabric_prompt,
    },
    "deployment": {},
    "created_at": now,
    "updated_at": now,
    "projectid": ALL_UP_FABRIC_ID,
}


# 4) Products MLV Agent (standalone)
products_mlv = {
    "id": PRODUCTS_MLV_ID,
    "type": "agent_project",
    "name": "Products MLV Agent",
    "description": "Specialized agent for product catalog questions using the products_mlvs Fabric GraphQL materialized logical view.",
    "deployment_mode": "standalone",
    "orchestrator": {
        "name": "Fabric Orchestrator",
        "description": "",
        "prompt": "",
        "model_config": _empty_model_config(),
        "subagents": [],
    },
    "standalone_agent": {
        "name": "Products MLV Agent",
        "description": "Queries only the products_mlvs table via Fabric GraphQL.",
        "semantic_model": _empty_semantic_model(),
        "model_config": _empty_model_config(),
        "prompt": products_mlv_prompt,
    },
    "deployment": {},
    "created_at": now,
    "updated_at": now,
    "projectid": PRODUCTS_MLV_ID,
}


# 5) Sales Order MLV Agent (standalone)
sales_order_mlv = {
    "id": SALES_ORDER_MLV_ID,
    "type": "agent_project",
    "name": "Sales Order MLV Agent",
    "description": "Specialized agent for sales order questions using the salesorders_mlvs Fabric GraphQL materialized logical view.",
    "deployment_mode": "standalone",
    "orchestrator": {
        "name": "Fabric Orchestrator",
        "description": "",
        "prompt": "",
        "model_config": _empty_model_config(),
        "subagents": [],
    },
    "standalone_agent": {
        "name": "Sales Order MLV Agent",
        "description": "Queries only the salesorders_mlvs table via Fabric GraphQL.",
        "semantic_model": _empty_semantic_model(),
        "model_config": _empty_model_config(),
        "prompt": sales_order_mlv_prompt,
    },
    "deployment": {},
    "created_at": now,
    "updated_at": now,
    "projectid": SALES_ORDER_MLV_ID,
}


# ---------------------------------------------------------------------------
# Push to Cosmos
# ---------------------------------------------------------------------------
def main():
    endpoint = os.environ["AGENT_MGMT_COSMOS_ENDPOINT"]
    database = os.environ["AGENT_MGMT_COSMOS_DATABASE"]
    container_name = os.environ["AGENT_MGMT_COSMOS_CONTAINER"]
    tenant = os.environ["AZURE_TENANT_ID"]
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")

    cred = ClientSecretCredential(tenant_id=tenant, client_id=client_id, client_secret=client_secret)
    client = CosmosClient(endpoint, credential=cred)
    db = client.get_database_client(database)
    container = db.get_container_client(container_name)

    # --- 1. Upsert AdventureWorks Orchestrator ---
    print(f"Upserting: {adventureworks_orchestrator['name']} ({ORCHESTRATOR_ID})")
    container.upsert_item(adventureworks_orchestrator)
    print("  OK")

    # --- 2. Update Bakehouse Agent (merge with existing) ---
    print(f"Updating: Bakehouse Agent ({BAKEHOUSE_ID})")
    try:
        existing = container.read_item(item=BAKEHOUSE_ID, partition_key=BAKEHOUSE_ID)
        existing.update(bakehouse_update)
        container.upsert_item(existing)
        print("  OK (type fixed to agent_project)")
    except Exception as e:
        print(f"  WARNING: could not update Bakehouse - {e}")

    # --- 3. Upsert All-up Fabric Agent ---
    print(f"Upserting: {all_up_fabric['name']} ({ALL_UP_FABRIC_ID})")
    container.upsert_item(all_up_fabric)
    print("  OK")

    # --- 4. Upsert Products MLV Agent ---
    print(f"Upserting: {products_mlv['name']} ({PRODUCTS_MLV_ID})")
    container.upsert_item(products_mlv)
    print("  OK")

    # --- 5. Upsert Sales Order MLV Agent ---
    print(f"Upserting: {sales_order_mlv['name']} ({SALES_ORDER_MLV_ID})")
    container.upsert_item(sales_order_mlv)
    print("  OK")

    # --- Verify ---
    print("\n--- All projects in Cosmos ---")
    items = list(container.query_items(
        "SELECT c.id, c.type, c.name, c.deployment_mode FROM c WHERE c.type = 'agent_project'",
        enable_cross_partition_query=True,
    ))
    for item in items:
        print(f"  [{item['deployment_mode']}] {item['name']} ({item['id'][:12]}...)")
    print(f"\nTotal: {len(items)} projects")


if __name__ == "__main__":
    main()
