# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Pure, dependency-free security helpers for Leafy.

No ADK / google-genai imports here on purpose — these are plain string/regex
functions so they stay fast and deterministic to unit test (see
tests/unit/test_guardrails.py). The ADK-facing callback that wires these into
the agent lives in app/security/callback.py.
"""

import re
from typing import Pattern

# ---------------------------------------------------------------------------
# Prompt-injection screen
# ---------------------------------------------------------------------------
# Each rule is (compiled pattern, human-readable reason). Patterns target
# attempts to override Leafy's own instructions, extract its system prompt,
# redefine its role, or bypass rules like "confirm before saving" — not
# ordinary plant-care language.
_INJECTION_RULES: list[tuple[Pattern[str], str]] = [
    (
        re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+instructions?", re.I),
        "attempt to override prior instructions",
    ),
    (
        re.compile(r"disregard\s+(all\s+|any\s+)?(previous|prior|above|earlier|your)\s+(instructions?|rules?)", re.I),
        "attempt to override prior instructions",
    ),
    (
        re.compile(r"forget\s+(all\s+)?(your\s+)?(previous\s+)?(instructions?|rules?|training)", re.I),
        "attempt to override prior instructions",
    ),
    (
        re.compile(r"(reveal|show|print|leak)\s+(me\s+)?(your\s+)?(system\s+prompt|internal\s+instructions?|instructions?)", re.I),
        "attempt to extract the system prompt",
    ),
    (
        re.compile(r"what\s+(are|is)\s+your\s+(system\s+prompt|instructions?|rules?)", re.I),
        "attempt to extract the system prompt",
    ),
    (re.compile(r"\bbypass(es|ing|ed)?\b", re.I), "attempt to bypass safety rules"),
    (re.compile(r"\bjailbreak(s|ing|ed)?\b", re.I), "attempt to bypass safety rules"),
    (re.compile(r"\bdan\s+mode\b", re.I), "attempt to bypass safety rules"),
    (re.compile(r"\bdo\s+anything\s+now\b", re.I), "attempt to bypass safety rules"),
    (
        re.compile(r"you\s+are\s+now\s+(a|an)\b", re.I),
        "attempt to redefine the assistant's identity/role",
    ),
    (
        re.compile(r"act\s+as\s+(if\s+you\s+(are|were)|a|an)\b", re.I),
        "attempt to redefine the assistant's identity/role",
    ),
    (
        re.compile(r"pretend\s+(you\s+are|to\s+be)\b", re.I),
        "attempt to redefine the assistant's identity/role",
    ),
    (
        re.compile(r"override\s+(your\s+)?(rules?|instructions?|behaviou?r)", re.I),
        "attempt to override safety rules",
    ),
    (
        re.compile(r"auto[- ]?approve", re.I),
        "attempt to auto-approve actions without confirmation",
    ),
    (
        re.compile(r"(without|no need for|skip)\s+(asking|confirmation|confirming|your confirmation)", re.I),
        "attempt to skip required confirmation",
    ),
    (
        re.compile(r"^\s*system\s*:", re.I),
        "attempt to inject a fake system message",
    ),
    (
        re.compile(r"new\s+instructions?\s*:", re.I),
        "attempt to inject new instructions",
    ),
]


def detect_prompt_injection(text: str) -> tuple[bool, str]:
    """Scans user-supplied text for prompt-injection attempts.

    Args:
        text: The raw user message text to scan.

    Returns:
        (True, reason) if an injection attempt is detected, else (False, "").
    """
    if not text:
        return False, ""
    for pattern, reason in _INJECTION_RULES:
        if pattern.search(text):
            return True, reason
    return False, ""


# ---------------------------------------------------------------------------
# PII redaction (for logging only — real values still flow to tools/model)
# ---------------------------------------------------------------------------
_COORD_PAIR_RE = re.compile(r"-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}")
_COORD_KV_RE = re.compile(r"(?i)\b(lat(?:itude)?|lon(?:gitude)?)\b(\"?\s*[:=]\s*)-?\d{1,3}(?:\.\d+)?")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_CARD_RE = re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{1,4}(?!\d)|(?<!\d)\d{13,19}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")


def redact_pii(text: str) -> str:
    """Redacts coordinate-like values, emails, phone numbers, and card-like
    digit sequences from text. Intended for log output — do not use this on
    data passed to tools (e.g. real coordinates must still reach get_weather).

    Args:
        text: The raw text to redact.

    Returns:
        The text with sensitive-looking substrings replaced by tokens like
        '[REDACTED_COORDS]', '[REDACTED_EMAIL]', '[REDACTED_PHONE]',
        '[REDACTED_CARD]'.
    """
    if not text:
        return text
    redacted = text
    redacted = _COORD_PAIR_RE.sub("[REDACTED_COORDS]", redacted)
    redacted = _COORD_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_COORDS]", redacted)
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    # Card-like digit runs before phone numbers, so a long card number isn't
    # partially consumed by the shorter phone pattern first.
    redacted = _CARD_RE.sub("[REDACTED_CARD]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    return redacted


# ---------------------------------------------------------------------------
# Light tier safety scrubber (prevent exposing numeric internal light tiers)
# ---------------------------------------------------------------------------
_LIGHT_TIER_RE = re.compile(r"\b(?:light\s+)?tier\s+(?:of\s+)?([0-3])\b", re.I)

_LIGHT_TIER_MAPPING = {
    "0": "low light/shade",
    "1": "medium indirect light",
    "2": "bright indirect light",
    "3": "bright direct light"
}


def cleanse_light_tiers(text: str) -> str:
    """Scrubs numeric internal light tiers from text, replacing them with
    human-readable equivalents so that technical implementation details are
    never exposed to the user.

    Args:
        text: The text to cleanse.

    Returns:
        Cleaned text with numeric tiers replaced.
    """
    if not text:
        return text
    def replace_match(match):
        num = match.group(1)
        return _LIGHT_TIER_MAPPING.get(num, match.group(0))
    return _LIGHT_TIER_RE.sub(replace_match, text)


def detect_sql_or_command_injection(text: str) -> tuple[bool, str]:
    """Scans user-supplied text for SQL-injection or command-injection probes.

    Args:
        text: The raw user message text to scan.

    Returns:
        (True, reason) if an injection pattern is detected, else (False, "").
    """
    if not text:
        return False, ""

    normalized = text.strip()

    # 1. SQL comments
    if "--" in normalized:
        return True, "SQL comment character detected"

    # 2. Boolean identities
    if re.search(r"\b\d+\s*=\s*\d+\b", normalized) or re.search(r"1\s*=\s*1", normalized):
        return True, "SQL boolean identity detected"

    # 3. OR ' / OR "
    if re.search(r"\bOR\s+['\"]", normalized, re.I):
        return True, "SQL logical OR injection pattern detected"

    # 4. Table name reference (from plants)
    if re.search(r"\bFROM\s+PLANTS\b", normalized, re.I):
        return True, "database table plants reference detected"

    # 5. SQL Keywords (case-insensitive SELECT, INSERT, DROP, ALTER, PRAGMA, UNION)
    for kw in ["SELECT", "INSERT", "DROP", "ALTER", "PRAGMA", "UNION"]:
        if re.search(r"\b" + kw + r"\b", normalized, re.I):
            return True, f"SQL keyword {kw} detected"

    # 6. UPDATE/DELETE commands (exact match for single keywords, or followed by SQL syntax)
    if normalized == "UPDATE" or re.search(r"\bUPDATE\s+\w+\s+SET\b", normalized, re.I):
        return True, "SQL UPDATE command detected"
    if normalized == "DELETE" or re.search(r"\bDELETE\s+FROM\b", normalized, re.I):
        return True, "SQL DELETE command detected"

    # 7. Semicolon-terminated statement pattern
    if ";" in normalized:
        # Check if the semicolon is followed by an SQL command or comment
        if re.search(r";\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|PRAGMA|UNION|--)\b", normalized, re.I):
            return True, "chained SQL statement detected"
        # Check if it ends with a semicolon
        if normalized.endswith(";"):
            return True, "semicolon-terminated SQL statement detected"

    return False, ""


_WEATHER_CAT_RE = re.compile(r"(\(?)\bcategory\s+([0-4])(\)?)", re.I)
_WEATHER_CAT_NAMES = {
    "0": "sunny",
    "1": "cloudy",
    "2": "rainy",
    "3": "thunderstorm",
    "4": "snow",
}


def cleanse_weather_details(text: str) -> str:
    """Scrubs numeric internal weather categories and temperature thresholds,
    replacing them with plain words.
    """
    if not text:
        return text

    # Remove category name followed by category number in parentheses first (e.g. "snow (category 4)" -> "snow")
    text = re.sub(
        r"\b(sunny|cloudy|rain|rainy|thunderstorm|storm|snow|snowy)\s+\(category\s+[0-4]\)",
        r"\1",
        text,
        flags=re.I
    )

    def replace_category(match):
        open_paren = match.group(1)
        num = match.group(2)
        word = _WEATHER_CAT_NAMES.get(num, "unknown")
        return f"({word})" if open_paren == "(" else word

    text = _WEATHER_CAT_RE.sub(replace_category, text)

    # Scrub temperature thresholds like "down to -15°C" or "down to 5°C"
    def replace_temp_threshold(match):
        temp_val = float(match.group(1))
        if temp_val < 0:
            return "down to freezing conditions"
        elif temp_val <= 10:
            return "down to cool temperatures"
        elif temp_val <= 15:
            return "down to mild temperatures"
        else:
            return "down to warm temperatures"

    text = re.sub(r"\bdown\s+to\s+(-?\d+(?:\.\d+)?)\s*°C", replace_temp_threshold, text, flags=re.I)
    return text


