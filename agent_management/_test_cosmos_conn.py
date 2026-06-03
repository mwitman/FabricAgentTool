"""Quick Cosmos DB connectivity test."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "")
database = os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
container = os.environ.get("AGENT_MGMT_COSMOS_CONTAINER", "agentmetadata")
print(f"Endpoint: {endpoint}")
print(f"Database: {database}")
print(f"Container: {container}")

if not endpoint:
    print("ERROR: AGENT_MGMT_COSMOS_ENDPOINT not set")
    exit(1)

from azure.identity import ClientSecretCredential
from azure.cosmos import CosmosClient

tenant = os.environ.get("AZURE_TENANT_ID", "")
client_id = os.environ.get("APP_CLIENT_ID", "")
client_secret = os.environ.get("APP_CLIENT_SECRET", "")
print(f"Tenant: {tenant[:8]}..." if tenant else "ERROR: No AZURE_TENANT_ID")
print(f"Client ID: {client_id[:8]}..." if client_id else "ERROR: No APP_CLIENT_ID")
print(f"Client Secret: {'set' if client_secret else 'ERROR: Not set'}")

try:
    cred = ClientSecretCredential(tenant_id=tenant, client_id=client_id, client_secret=client_secret)
    client = CosmosClient(endpoint, credential=cred)
    db = client.get_database_client(database)
    cont = db.get_container_client(container)
    items = list(cont.query_items("SELECT c.id, c.name FROM c", enable_cross_partition_query=True, max_item_count=5))
    print(f"\nSUCCESS: Found {len(items)} projects")
    for item in items[:5]:
        print(f"  - {item.get('id')}: {item.get('name', '(no name)')}")
except Exception as e:
    print(f"\nERROR: {type(e).__name__}: {e}")
