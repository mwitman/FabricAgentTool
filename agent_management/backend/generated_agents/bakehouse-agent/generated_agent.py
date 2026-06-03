from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

PROJECT = {
  "id": "8498bd90-6fc1-4242-b361-3cfcd0b280ed",
  "type": "agent_project",
  "name": "Bakehouse Agent",
  "description": "An agent over the Bakehouse semantic model",
  "deployment_mode": "standalone",
  "orchestrator": {
    "name": "Orchestrator",
    "description": "Routes business questions to semantic-model subagents.",
    "prompt": "",
    "subagents": []
  },
  "standalone_agent": {
    "name": "Bakehouse Standalone Agent",
    "description": "The Bakehouse Agent answers questions about Bakehouse franchises such as customer details and sales.",
    "semantic_model": {
      "workspace_id": "8bd167bf-a6af-4457-ac4d-f77b199a5dbf",
      "workspace_name": "DatabricksUnity",
      "semantic_model_id": "c5680f0a-0137-4534-99e1-ffce6e56a121",
      "semantic_model_name": "CustomerSales"
    },
    "prompt": "You are **Bakehouse Standalone Agent**, a Microsoft Fabric semantic model agent for the **CustomerSales** semantic model in the **DatabricksUnity** workspace.\n\n## Mission\nAnswer questions about **Bakehouse franchises**, including **customer details** and **sales**.\n\n## Operating rules\n- Always begin by inspecting the semantic model metadata to identify relevant tables, columns, measures, hierarchies, and relationships.\n- Use the metadata to determine the correct business entities and field names before writing any query.\n- Write only **read-only DAX** queries against the semantic model.\n- Execute the DAX query and base your answer **only on the returned results**.\n- Do not rely on prior assumptions, unstated schema knowledge, or external information.\n- If the metadata or query results are insufficient to answer confidently, say so clearly.\n\n## Query requirements\n- Prefer the simplest valid DAX that answers the question accurately.\n- Use existing measures when appropriate.\n- Apply filters explicitly when the user requests a specific customer, franchise, date period, or sales slice.\n- When summarizing or aggregating, ensure the grouping and calculation match the user\u2019s request.\n- Never perform write operations or suggest that data was modified.\n\n## Response requirements\n- Provide a concise, direct answer grounded in the query results.\n- Include key figures, relevant dimensions, and time periods when applicable.\n- If helpful, briefly summarize how the result was derived from the semantic model.\n- If a request is ambiguous, ask a clarifying question before querying."
  },
  "deployment": {},
  "created_at": "2026-05-20T18:32:57.347924+00:00",
  "updated_at": "2026-05-20T18:38:20.394965+00:00"
}
app = FastAPI(title=PROJECT.get("name", "Generated Agent"))


@app.get("/readiness")
async def readiness():
    return {"status": "ok", "service": "generated-agent", "project": PROJECT.get("name")}


@app.post("/responses")
@app.post("/v1/responses")
@app.post("/openai/v1/responses")
async def responses(request: Request):
    body = await request.json()
    message = _extract_input_text(body)
    conversation_id = body.get("conversation_id") or body.get("metadata", {}).get("conversation_id") or "default"
    text = _simulate_response(message)
    if body.get("stream") is True:
        return StreamingResponse(_stream(text, conversation_id), media_type="text/event-stream")
    return {"id": conversation_id, "output_text": text, "metadata": {"conversation_id": conversation_id}}


def _simulate_response(message: str) -> str:
    mode = PROJECT.get("deployment_mode")
    if mode == "standalone":
        agent = PROJECT.get("standalone_agent", {})
        model = agent.get("semantic_model", {}).get("semantic_model_name", "configured semantic model")
        return f"{agent.get('name', 'Standalone agent')} would answer using {model}. Runtime DAX execution is wired by deployment templates. User asked: {message}"
    subagents = PROJECT.get("orchestrator", {}).get("subagents", [])
    chosen = subagents[0] if subagents else {"name": "No subagent", "semantic_model": {"semantic_model_name": "none"}}
    model = chosen.get("semantic_model", {}).get("semantic_model_name", "configured semantic model")
    return f"{PROJECT.get('orchestrator', {}).get('name', 'Orchestrator')} routes to {chosen.get('name')} using {model}. Runtime DAX execution is wired by deployment templates. User asked: {message}"


async def _stream(text: str, conversation_id: str):
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': conversation_id, 'status': 'in_progress'}})}\n\n"
    yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': text})}\n\n"
    yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': conversation_id, 'status': 'completed'}})}\n\n"
    yield "data: [DONE]\n\n"


def _extract_input_text(body: dict[str, Any]) -> str:
    value = body.get("input") or body.get("message") or body.get("inputText")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.extend(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return "\n".join(parts)
    return ""
