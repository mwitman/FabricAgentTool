"""One-time script to create the AdventureWorks project in the local store."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT_API = ROOT.parent / "agent_api"


def read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


orch_prompt = read_prompt(AGENT_API / "orchestrator_agent" / "prompts" / "orchestrator_agent.md")
sales_prompt = read_prompt(AGENT_API / "orchestrator_agent" / "prompts" / "sales_agent.md")
customer_prompt = read_prompt(AGENT_API / "orchestrator_agent" / "prompts" / "customer_agent.md")
product_prompt = read_prompt(AGENT_API / "orchestrator_agent" / "prompts" / "product_agent.md")

project = {
    "id": "ae2c44be-df0c-4c6f-a68d-0805c080eda6",
    "type": "agent_project",
    "name": "AdventureWorks GraphQL Agents",
    "description": "Orchestrates Sales, Customer, and Product sub-agents that query Fabric GraphQL APIs for AdventureWorks data.",
    "deployment_mode": "orchestrator",
    "orchestrator": {
        "name": "GraphQL Agents Orchestrator",
        "description": "Orchestrates three sub-agents for Sales, Customer, and Product data via Fabric GraphQL.",
        "prompt": orch_prompt,
        "model_config": {"deployment_name": "", "model_display_name": ""},
        "subagents": [
            {
                "id": "sales-agent",
                "name": "Sales Agent",
                "description": "Queries sales data (orders, order details, totals, status) via Fabric GraphQL.",
                "semantic_model": {"workspace_id": "", "workspace_name": "", "semantic_model_id": "", "semantic_model_name": ""},
                "model_config": {"deployment_name": "", "model_display_name": ""},
                "prompt": sales_prompt,
                "guardrails": [],
            },
            {
                "id": "customer-agent",
                "name": "Customer Agent",
                "description": "Queries customer data (identity, addresses) via Fabric GraphQL.",
                "semantic_model": {"workspace_id": "", "workspace_name": "", "semantic_model_id": "", "semantic_model_name": ""},
                "model_config": {"deployment_name": "", "model_display_name": ""},
                "prompt": customer_prompt,
                "guardrails": [],
            },
            {
                "id": "product-agent",
                "name": "Product Agent",
                "description": "Queries product data (products, categories, models, descriptions) via Fabric GraphQL.",
                "semantic_model": {"workspace_id": "", "workspace_name": "", "semantic_model_id": "", "semantic_model_name": ""},
                "model_config": {"deployment_name": "", "model_display_name": ""},
                "prompt": product_prompt,
                "guardrails": [],
            },
        ],
    },
    "standalone_agent": {
        "name": "Standalone Fabric Agent",
        "description": "",
        "semantic_model": {"workspace_id": "", "workspace_name": "", "semantic_model_id": "", "semantic_model_name": ""},
        "model_config": {"deployment_name": "", "model_display_name": ""},
        "prompt": "",
    },
    "deployment": {},
    "projectid": "ae2c44be-df0c-4c6f-a68d-0805c080eda6",
}

data_dir = ROOT / "backend" / "data" / "projects"
data_dir.mkdir(parents=True, exist_ok=True)
path = data_dir / f"{project['id']}.json"
path.write_text(json.dumps(project, indent=2), encoding="utf-8")
print(f"Project saved to {path}")
print(f"Project ID: {project['id']}")
print(f"Subagents: {len(project['orchestrator']['subagents'])}")
