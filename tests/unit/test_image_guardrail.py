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
Deterministic unit tests for app/security/image_guardrail.py - no LLM, no
quota, no network. Pure magic-byte / MIME-type logic only.
"""
import pytest

from app.security.image_guardrail import MAX_IMAGE_BYTES, is_allowed_image

# Minimal real magic-byte headers, padded so length checks behave sensibly.
_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_WEBP_HEADER = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 32
_PDF_HEADER = b"%PDF-1.4\n" + b"\x00" * 32
_SCRIPT_CONTENT = b"<script>alert('hi')</script>"


# ---------------------------------------------------------------------------
# Valid images pass
# ---------------------------------------------------------------------------
def test_valid_jpeg_is_allowed():
    allowed, reason = is_allowed_image("image/jpeg", _JPEG_HEADER)
    assert allowed is True
    assert reason == ""


def test_valid_png_is_allowed():
    allowed, reason = is_allowed_image("image/png", _PNG_HEADER)
    assert allowed is True
    assert reason == ""


def test_valid_webp_is_allowed():
    allowed, reason = is_allowed_image("image/webp", _WEBP_HEADER)
    assert allowed is True
    assert reason == ""


def test_declared_mime_type_is_case_insensitive():
    allowed, reason = is_allowed_image("IMAGE/JPEG", _JPEG_HEADER)
    assert allowed is True


# ---------------------------------------------------------------------------
# Non-image content is rejected
# ---------------------------------------------------------------------------
def test_script_content_rejected():
    allowed, reason = is_allowed_image("image/jpeg", _SCRIPT_CONTENT)
    assert allowed is False
    assert "JPG" in reason or "PNG" in reason or "WEBP" in reason


def test_pdf_content_rejected_even_with_image_mime_type():
    allowed, reason = is_allowed_image("image/jpeg", _PDF_HEADER)
    assert allowed is False


def test_pdf_declared_mime_type_rejected_outright():
    allowed, reason = is_allowed_image("application/pdf", _PDF_HEADER)
    assert allowed is False


def test_unsupported_declared_mime_type_rejected():
    allowed, reason = is_allowed_image("text/plain", b"hello world")
    assert allowed is False


def test_empty_bytes_rejected():
    allowed, reason = is_allowed_image("image/jpeg", b"")
    assert allowed is False


# ---------------------------------------------------------------------------
# Declared-vs-actual mismatch is rejected (don't trust declared type alone)
# ---------------------------------------------------------------------------
def test_png_bytes_declared_as_jpeg_rejected():
    allowed, reason = is_allowed_image("image/jpeg", _PNG_HEADER)
    assert allowed is False


def test_jpeg_bytes_declared_as_png_rejected():
    allowed, reason = is_allowed_image("image/png", _JPEG_HEADER)
    assert allowed is False


def test_webp_bytes_declared_as_jpeg_rejected():
    allowed, reason = is_allowed_image("image/jpeg", _WEBP_HEADER)
    assert allowed is False


# ---------------------------------------------------------------------------
# Oversized files are rejected
# ---------------------------------------------------------------------------
def test_oversized_valid_jpeg_rejected():
    oversized = _JPEG_HEADER + b"\x00" * MAX_IMAGE_BYTES
    allowed, reason = is_allowed_image("image/jpeg", oversized)
    assert allowed is False
    assert "large" in reason.lower()


def test_at_size_limit_valid_jpeg_allowed():
    exactly_at_limit = _JPEG_HEADER + b"\x00" * (MAX_IMAGE_BYTES - len(_JPEG_HEADER))
    assert len(exactly_at_limit) == MAX_IMAGE_BYTES
    allowed, reason = is_allowed_image("image/jpeg", exactly_at_limit)
    assert allowed is True


# ---------------------------------------------------------------------------
# Reasons are always friendly, user-facing strings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mime_type,data", [
    ("image/jpeg", _SCRIPT_CONTENT),
    ("application/pdf", _PDF_HEADER),
    ("image/jpeg", _PNG_HEADER),
    ("image/jpeg", b""),
])
def test_rejection_reasons_ask_for_a_photo(mime_type, data):
    allowed, reason = is_allowed_image(mime_type, data)
    assert allowed is False
    assert isinstance(reason, str) and reason
    assert "photo" in reason.lower()
