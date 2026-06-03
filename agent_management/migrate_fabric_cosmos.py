from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
from typing import Any

from azure.cosmos import CosmosClient
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv

DEFAULT_SOURCE_ENDPOINT = "https://b46a4038-da7f-4a7b-8ad9-9fb67c4f8f72.zb4.sql.cosmos.fabric.microsoft.com:443/"
SOURCE_DATABASE = "Cosmos-StitchAgentTool"
SOURCE_PROJECT_CONTAINER = "AgentManagement"
SOURCE_PERMISSIONS_CONTAINER = "Permissions"


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy Agent Management data from Fabric Cosmos DB to Azure Cosmos DB.")
    parser.add_argument("--dry-run", action="store_true", help="Read and report documents without writing to Azure Cosmos DB.")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")

    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["APP_CLIENT_ID"],
        client_secret=os.environ["APP_CLIENT_SECRET"],
    )

    source_endpoint = os.environ.get("FABRIC_COSMOS_SOURCE_ENDPOINT", DEFAULT_SOURCE_ENDPOINT)
    target_endpoint = os.environ["AGENT_MGMT_COSMOS_ENDPOINT"]

    source_client = CosmosClient(source_endpoint, credential=credential)
    target_client = CosmosClient(target_endpoint, credential=credential)

    project_count = migrate_container(
        source_client=source_client,
        target_client=target_client,
        source_database=SOURCE_DATABASE,
        source_container=SOURCE_PROJECT_CONTAINER,
        target_database=os.environ["AGENT_MGMT_COSMOS_DATABASE"],
        target_container=os.environ["AGENT_MGMT_COSMOS_CONTAINER"],
        partition_field=os.environ.get("AGENT_MGMT_COSMOS_PARTITION_KEY", "/projectid").strip("/") or "projectid",
        dry_run=args.dry_run,
    )
    permissions_count = migrate_container(
        source_client=source_client,
        target_client=target_client,
        source_database=SOURCE_DATABASE,
        source_container=SOURCE_PERMISSIONS_CONTAINER,
        target_database=os.environ["AGENT_MGMT_PERMISSIONS_DATABASE"],
        target_container=os.environ["AGENT_MGMT_PERMISSIONS_CONTAINER"],
        partition_field=os.environ.get("AGENT_MGMT_PERMISSIONS_PARTITION_KEY", "/roleid").strip("/") or "roleid",
        dry_run=args.dry_run,
    )

    action = "Would copy" if args.dry_run else "Copied"
    print(f"{action} {project_count} project documents.")
    print(f"{action} {permissions_count} permission documents.")


def migrate_container(
    *,
    source_client: CosmosClient,
    target_client: CosmosClient,
    source_database: str,
    source_container: str,
    target_database: str,
    target_container: str,
    partition_field: str,
    dry_run: bool,
) -> int:
    source = source_client.get_database_client(source_database).get_container_client(source_container)
    target = target_client.get_database_client(target_database).get_container_client(target_container)
    count = 0

    for source_item in source.query_items(query="SELECT * FROM c", enable_cross_partition_query=True):
        item = clean_item(source_item)
        item[partition_field] = item["id"]
        if not dry_run:
            target.upsert_item(item)
        count += 1

    return count


def clean_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in item.items() if not key.startswith("_")}


if __name__ == "__main__":
    main()
