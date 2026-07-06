"""LLM-as-judge metric for routing/behaviour correctness."""
from google import genai
from google.genai import types
from pydantic import BaseModel

class Verdict(BaseModel):
    score: int
    explanation: str

def _extract_text(content_obj) -> str:
    """Pull plain text from a Content object or return str(obj)."""
    if content_obj is None:
        return ""
    if hasattr(content_obj, "parts") and content_obj.parts:
        return " ".join(p.text for p in content_obj.parts if getattr(p, "text", None))
    return str(content_obj)


def evaluate(instance):
    # instance is a Pydantic EvalCase object, not a dict
    user_query = _extract_text(getattr(instance, "prompt", None))
    agent_resp = _extract_text(
        getattr(getattr(instance, "responses", [None])[0], "response", None)
        if getattr(instance, "responses", None) else None
    )
    agent_data = str(getattr(instance, "agent_data", ""))
    prompt = (
        "You are a QA judge. Rate the agent's routing/behaviour correctness on a 1-5 scale.\n"
        "Criteria:\n"
        "- Did the agent gather required information (location, plant, last-watered date, placement) before advising?\n"
        "- Did it use the right tools (e.g., list_plants, get_weather, watering_reasoner, geocode)?\n"
        "- Score 5: Gathered all required information, called the correct tools in the correct order, and resolved queries logically.\n"
        "- Score 1: Hallucinated data, skipped required tools, or gave recommendations without gathering information.\n\n"
        f"User Initial Query: {user_query}\n"
        f"Agent Final Response: {agent_resp}\n"
        f"Full Trace: {agent_data}\n"
    )
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=Verdict,
        ),
    )
    verdict = response.parsed
    if verdict is None:
        return {"score": 1, "explanation": response.text or "Error parsing verdict"}
    return {"score": max(1, min(5, verdict.score)), "explanation": verdict.explanation}
