"""Quick test: call getDefinition and run the enhanced TMDL parser locally."""
import asyncio
import json
import os
import sys

import aiohttp
from azure.identity import ClientSecretCredential

# Load .env
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from hosted_agent_runtime.app import (
    _decode_definition_payload,
    _tables_from_tmdl,
    _relationships_from_tmdl,
    _ai_instructions_from_parts,
    _merge_tables,
)

FABRIC_API = "https://api.fabric.microsoft.com/v1"
WORKSPACE_ID = os.getenv("TEST_WORKSPACE_ID", "")
MODEL_ID = os.getenv("TEST_SEMANTIC_MODEL_ID", "")


async def main():
    if not WORKSPACE_ID or not MODEL_ID:
        print("Set TEST_WORKSPACE_ID and TEST_SEMANTIC_MODEL_ID in .env or environment")
        return

    tenant_id = os.getenv("AZURE_TENANT_ID", "")
    client_id = os.getenv("APP_CLIENT_ID", "")
    client_secret = os.getenv("APP_CLIENT_SECRET", "")

    if not all([tenant_id, client_id, client_secret]):
        print("Set AZURE_TENANT_ID, APP_CLIENT_ID, APP_CLIENT_SECRET")
        return

    credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    token = credential.get_token("https://api.fabric.microsoft.com/.default").token

    url = f"{FABRIC_API}/workspaces/{WORKSPACE_ID}/items/{MODEL_ID}/getDefinition?format=TMDL"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as resp:
            if resp.status == 202:
                # Long-running operation
                location = resp.headers.get("Location") or resp.headers.get("Operation-Location")
                print(f"Long-running op: {location}")
                print("Waiting...")
                for _ in range(30):
                    await asyncio.sleep(2)
                    async with session.get(location, headers=headers) as poll_resp:
                        poll = await poll_resp.json()
                        status = str(poll.get("status", "")).lower()
                        if status in ("succeeded", "completed"):
                            result_url = poll_resp.headers.get("Location") or poll.get("resultUrl")
                            if result_url:
                                async with session.get(result_url, headers=headers) as result_resp:
                                    payload = await result_resp.json()
                            else:
                                payload = poll
                            break
                        elif status in ("failed", "cancelled"):
                            print(f"Failed: {poll}")
                            return
                else:
                    print("Timed out waiting for getDefinition")
                    return
            elif resp.status >= 400:
                text = await resp.text()
                print(f"Error {resp.status}: {text}")
                return
            else:
                payload = await resp.json()

    parts = payload.get("definition", {}).get("parts") or payload.get("parts") or []
    print(f"\n--- {len(parts)} parts returned ---")
    for p in parts:
        print(f"  {p.get('path')}")

    # Parse tables
    tables: list = []
    relationships: list = []
    for part in parts:
        path = str(part.get("path") or "")
        if not path.endswith(".tmdl"):
            continue
        text = _decode_definition_payload(part)
        if text:
            tables.extend(_tables_from_tmdl(text))
            relationships.extend(_relationships_from_tmdl(text))

    merged = _merge_tables(tables)
    ai_instructions = _ai_instructions_from_parts(parts)

    result = {
        "workspace_id": WORKSPACE_ID,
        "semantic_model_id": MODEL_ID,
        "tables": merged,
        "relationships": relationships,
        "ai_instructions": ai_instructions,
    }

    print("\n--- RESULT ---")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
