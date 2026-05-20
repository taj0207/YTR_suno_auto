"""Thin wrapper around google-generativeai. One place to swap model / add retry."""
from __future__ import annotations

import os

import google.generativeai as genai

DEFAULT_MODEL = "gemini-2.5-pro"

_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set (see .env)")
    genai.configure(api_key=api_key)
    _configured = True


def generate(prompt: str, *, model: str = DEFAULT_MODEL, temperature: float = 0.9) -> str:
    _ensure_configured()
    m = genai.GenerativeModel(model)
    resp = m.generate_content(
        prompt,
        generation_config={"temperature": temperature},
    )
    return resp.text
