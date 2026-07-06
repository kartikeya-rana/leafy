"""LLM-as-judge metric for recommendation quality."""
from google import genai
from google.genai import types
from pydantic import BaseModel

class Verdict(BaseModel):
    score: int
    explanation: str

def _extract_text(content_obj) -> str:
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
        "You are a QA judge. Rate the agent's recommendation quality on a 1-5 scale.\n"
        "Criteria:\n"
        "- If the user query is about watering (e.g., when to water, how much to water), is the watering advice sensible, logical, and tailored to the weather/plant?\n"
        "- If the query is about watering, does the response ALWAYS include a reminder/instructions to manually check/verify soil moisture first?\n"
        "- If the query is about watering and the plant isn't in the knowledge base (e.g. for Rose, which is not in the KB), does the response clearly flag the guidance as generic?\n"
        "- If the query is NOT about watering (e.g., adding a plant, shelter/moving plants, light/spot check, off-topic images, prompt injection, asking for location), rate 5/5 if the response is appropriate and correct for that query, and do not penalize for the absence of watering advice/moisture check notes.\n"
        "- Score 5: The response is appropriate for the query. For watering queries, it provides highly sensible advice, includes a clear manual soil-moisture check reminder, and correctly flags generic guidance if the species was not found in the KB.\n"
        "- Score 1: Poor/fabricated advice, misses the manual check note on a watering query, or presents generic guidance as specific database care.\n\n"
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
