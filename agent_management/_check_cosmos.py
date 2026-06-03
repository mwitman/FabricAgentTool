import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from azure.identity import ClientSecretCredential
from azure.cosmos import CosmosClient

cred = ClientSecretCredential(
    os.environ["AZURE_TENANT_ID"],
    os.environ["APP_CLIENT_ID"],
    os.environ["APP_CLIENT_SECRET"],
)
client = CosmosClient(os.environ["AGENT_MGMT_COSMOS_ENDPOINT"], credential=cred)
db = client.get_database_client(os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents"))
container = db.get_container_client(os.environ.get("AGENT_MGMT_COSMOS_CONTAINER", "agentmetadata"))

items = list(container.query_items("SELECT c.id, c.type, c.name FROM c", enable_cross_partition_query=True))
print(f"Total documents: {len(items)}")
for i in items:
    print(f"  {i['id']} | {i.get('type', 'NO TYPE')} | {i.get('name', 'NO NAME')}")
