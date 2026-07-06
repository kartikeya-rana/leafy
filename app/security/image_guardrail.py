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
Pure, dependency-free image validation for uploaded photos (e.g. for the
Spot/Light Check feature).

No ADK / google-genai imports here on purpose, so it stays fast to unit test.
The ADK callback that wires this in lives in app/security/callback.py.

This only checks that a file really is a JPEG/PNG/WEBP image, not its
content. It never trusts the caller-declared MIME type alone: it also
sniffs the actual file magic bytes and rejects a mismatch.
"""

ALLOWED_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 10 MB

_ASK_FOR_CLEAR_PHOTO = "Could you send a clear JPG, PNG, or WEBP photo instead?"


def _sniff_mime_type(data_bytes: bytes) -> str | None:
    """Detects the real image format from magic bytes, or None if the bytes
    don't look like any of the supported image formats."""
    if data_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data_bytes) >= 12 and data_bytes[0:4] == b"RIFF" and data_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_allowed_image(mime_type: str, data_bytes: bytes) -> tuple[bool, str]:
    """Validates an uploaded image before Leafy uses it.

    Accepts only real JPEG/PNG/WEBP images. The declared MIME type must be in
    the allowlist AND the file's actual magic bytes must sniff to a real
    image of that same type, so a mismatched, disguised, or non-image file
    (script, PDF, etc.) is rejected even if it claims to be a photo.

    Args:
        mime_type: The declared MIME type of the uploaded file.
        data_bytes: The raw file bytes.

    Returns:
        (True, "") if the image is allowed, else (False, reason). The reason
        is a friendly, user-facing message asking for a valid photo.
    """
    if not data_bytes:
        return False, f"That photo came through empty. {_ASK_FOR_CLEAR_PHOTO}"

    if len(data_bytes) > MAX_IMAGE_BYTES:
        return False, f"That photo is too large for me to use. {_ASK_FOR_CLEAR_PHOTO}"

    declared = (mime_type or "").strip().lower()
    if declared not in ALLOWED_MIME_TYPES:
        return False, (
            f"I can only use JPG, PNG, or WEBP photos, and {mime_type!r} isn't one of those. "
            f"{_ASK_FOR_CLEAR_PHOTO}"
        )

    sniffed = _sniff_mime_type(data_bytes)
    if sniffed is None:
        return False, f"That file doesn't actually look like a photo to me. {_ASK_FOR_CLEAR_PHOTO}"

    if sniffed != declared:
        return False, (
            f"That file is labeled {declared!r} but doesn't look like one. {_ASK_FOR_CLEAR_PHOTO}"
        )

    return True, ""
