import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Initialize DB
import app.storage.repository as repository
repository.init_db()

from app.agent import root_agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.memory import InMemoryMemoryService
from google.genai import types


def _content_to_dict(content) -> dict | None:
    """Convert a google.genai.types.Content to a plain dict."""
    if content is None:
        return None
    parts = []
    for part in (content.parts or []):
        if part.text is not None:
            parts.append({"text": part.text})
        elif part.function_call is not None:
            parts.append({
                "functionCall": {
                    "name": part.function_call.name,
                    "args": dict(part.function_call.args or {}),
                }
            })
        elif part.function_response is not None:
            parts.append({
                "functionResponse": {
                    "name": part.function_response.name,
                    "response": dict(part.function_response.response or {}),
                }
            })
    if not parts:
        return None
    return {"role": content.role or "model", "parts": parts}


async def run_scenario(scenario):
    eval_case_id = scenario["eval_case_id"]
    setup = scenario.get("setup", {})
    turns = scenario.get("turns", [])

    # 1. Reset and setup database
    conn = repository.get_db_connection()
    conn.execute("DELETE FROM plant_catalog;")
    conn.execute("DELETE FROM user_profiles;")
    conn.commit()
    conn.close()

    # Setup location
    loc = setup.get("location")
    if loc:
        repository.update_location(
            user_id="local_user",
            location_text=loc["location_text"],
            lat=loc["lat"],
            lon=loc["lon"],
            resolved_name=loc["resolved_name"]
        )

    # Setup plants
    for p in setup.get("plants", []):
        dt = datetime.fromisoformat(p["last_watered_date"].replace("Z", "+00:00")) if p.get("last_watered_date") else None
        repository.add_plant(
            user_id="local_user",
            species=p["species"],
            nickname=p.get("nickname"),
            placement=p["placement"],
            last_watered_date=dt,
            weather_tolerance=p.get("weather_tolerance")
        )

    # 2. Run turns using Runner
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    runner = Runner(
        app_name="app",
        agent=root_agent,
        session_service=session_service,
        memory_service=memory_service,
    )

    session = await session_service.create_session(user_id="local_user", app_name="app")

    final_text = ""
    for turn in turns:
        # A turn is either a plain string (text only), or a dict for turns
        # that also attach an image: {"text": ..., "image_base64": ...,
        # "image_mime_type": ...}. Image bytes are decoded fresh per turn so
        # they never sit around as plain text in the scenario dict.
        if isinstance(turn, dict):
            parts = []
            if turn.get("image_base64"):
                parts.append(types.Part(
                    inline_data=types.Blob(
                        mime_type=turn.get("image_mime_type", "image/png"),
                        data=base64.b64decode(turn["image_base64"]),
                    )
                ))
            if turn.get("text"):
                parts.append(types.Part.from_text(text=turn["text"]))
            message = types.Content(role="user", parts=parts)
        else:
            message = types.Content(role="user", parts=[types.Part.from_text(text=turn)])

        async for event in runner.run_async(
            new_message=message,
            user_id="local_user",
            session_id=session.id,
        ):
            if event.content and event.content.role == "model" and event.content.parts:
                texts = [part.text for part in event.content.parts if part.text]
                if texts:
                    final_text = "\n".join(texts)

    # 3. Retrieve events and structure them
    session = await session_service.get_session(app_name="app", session_id=session.id, user_id="local_user")

    # Group events by turn using author == "user"
    # Each AgentEvent only allows: author, content, event_time, state_delta, active_tools
    # Use camelCase aliases since validate_by_alias=True and serialize via JSON
    grouped_turns = []
    current_events = []
    for ev in session.events:
        if ev.author == "user":
            if current_events:
                grouped_turns.append({"events": current_events})
                current_events = []
        content_dict = _content_to_dict(ev.content)
        ev_dict = {"author": ev.author}
        if content_dict is not None:
            ev_dict["content"] = content_dict
        current_events.append(ev_dict)

    if current_events:
        grouped_turns.append({"events": current_events})

    # Clean up runner resources
    await runner.close()

    first_turn_text = turns[0].get("text", "") if isinstance(turns[0], dict) else turns[0]

    # Build EvalCase-compatible dict using camelCase keys (alias_generator=to_camel)
    # - prompt: Content dict
    # - responses: list of ResponseCandidate, field name is "response" (not "content")
    # - agentData: uses camelCase
    return {
        "evalCaseId": eval_case_id,
        "prompt": {"role": "user", "parts": [{"text": first_turn_text}]},
        "responses": [
            {"response": {"role": "model", "parts": [{"text": final_text}]}}
        ],
        "agentData": {
            "agents": {
                "Leafy": {"agentId": "Leafy"}
            },
            "turns": grouped_turns
        }
    }


async def main():
    dataset_path = "tests/eval/datasets/leafy-scenarios.json"
    with open(dataset_path, "r") as f:
        data = json.load(f)

    eval_cases = []
    for sc in data["scenarios"]:
        print(f"Running scenario: {sc['eval_case_id']} - {sc['description']}...")
        try:
            case_data = await run_scenario(sc)
            eval_cases.append(case_data)
            print(f"Scenario {sc['eval_case_id']} completed successfully.")
        except Exception as e:
            print(f"Error running scenario {sc['eval_case_id']}: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # Write traces
    out_dir = "tests/eval/artifacts"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "traces.json")
    payload = {"evalCases": eval_cases}
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    # Validate immediately so we catch schema mismatches before grade runs
    from vertexai._genai.types.common import EvaluationDataset
    with open(out_path) as f:
        content = f.read()
    EvaluationDataset.model_validate_json(content)
    print(f"All traces saved and validated against EvaluationDataset schema → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
