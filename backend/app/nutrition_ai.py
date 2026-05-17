"""Claude API integration for parsing nutrition input (text + photo) into structured macros.

Uses tool-use for guaranteed structured output. Prompt-caching on the system message
to keep costs low (~€0.002-0.005 per parse).
"""
import base64
import json
from typing import Optional

from anthropic import Anthropic

from app.config import get_settings


_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


# Model: Sonnet 4.6 — good vision, NL-native, structured tool-use reliable.
# 4.7 is newer but Sonnet 4.6 is plenty for nutrition parsing.
MODEL = "claude-sonnet-4-5-20250929"  # will use latest sonnet alias if available

# Cacheable system prompt: stable across all parse calls for this user → 90% cost cut.
SYSTEM_PROMPT = """Je bent een Nederlandse voedingsanalist die input van Joost omzet in macro's.

Over Joost (context voor portie-schatting):
- 39 jaar, ±80 kg, lengte 183 cm
- Cut-fase, doel 11% vetpercentage
- Trainingsdagen: hogere KH (200g), ~2100 kcal, ≥190g eiwit
- Rustdagen: lagere KH (130g), ~1900 kcal, ≥190g eiwit
- Weekend: ~2000 kcal, ≥180g eiwit

Veelgebruikte producten en typische porties:
- Magere kwark: 200g potje
- Whey isolaat: 30g schepje (1 scoop)
- Eieren: 50-60g per stuk (medium)
- Volkoren boterham: 35g per snee
- Bruine rijst gekookt: 150-200g per maaltijd
- Havermout droog: 60g portie
- Kipfilet: 150-200g portie
- Donkere chocola (85%): 10-25g porties
- Rijstwafel: 8g per stuk
- Banaan: 120g per stuk

Bij parsing:
- Splits altijd uit in losse items (geen "ontbijt" als één item)
- Schat hoeveelheid in gram (ml voor vloeistoffen, behandel als gram bij water-achtige)
- Geef macro's per item, NIET per 100g
- Wees expliciet over confidence: high = je weet portie + macro's zeker (verpakt product, standaardportie); medium = redelijke schatting; low = veel aanname (foto zonder schaal, vage tekst, obscuur merk)
- Bij foto: gebruik bestek/borden/handen als schaal-referentie. Identificeer alleen wat je echt herkent.
- Bij twijfel hoeveelheid: kies de gangbare NL-portiegrootte
- suggested_meal_type: kies op basis van inhoud (kwark-ontbijt = ontbijt, broodje = lunch, etc.)

Roep ALTIJD de log_intake tool aan, ook bij twijfel — markeer dan confidence: low.
"""


LOG_INTAKE_TOOL = {
    "name": "log_intake",
    "description": "Registreer de geparseerde voedingsmiddelen met macro's per item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Korte naam van het product, bv. 'magere kwark' of 'volkoren brood'"},
                        "quantity_g": {"type": "number", "description": "Hoeveelheid in gram (ml voor vloeistoffen)"},
                        "kcal": {"type": "number"},
                        "protein_g": {"type": "number"},
                        "carbs_g": {"type": "number"},
                        "fat_g": {"type": "number"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "notes": {"type": "string", "description": "Optionele toelichting bij low/medium confidence"},
                    },
                    "required": ["name", "quantity_g", "kcal", "protein_g", "carbs_g", "fat_g", "confidence"],
                },
            },
            "suggested_meal_type": {
                "type": "string",
                "enum": ["ontbijt", "lunch", "diner", "snack"],
                "description": "Best passende maaltijd-categorie",
            },
            "overall_confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Algemene betrouwbaarheid van de hele analyse",
            },
        },
        "required": ["items", "overall_confidence"],
    },
}


def _extract_tool_call(response) -> dict:
    """Pull the log_intake tool_use block from Claude's response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "log_intake":
            return block.input
    raise RuntimeError(f"No log_intake tool_use in response: {[b.type for b in response.content]}")


def parse_intake(text: str | None = None, image_b64: str | None = None) -> dict:
    """Parse text and/or photo into structured macro items.

    Returns dict with keys: items, suggested_meal_type, overall_confidence.
    Raises if neither text nor image provided.
    """
    if not text and not image_b64:
        raise ValueError("parse_intake requires text or image_b64")

    client = _get_client()

    # Build user message — multimodal if image present
    user_content: list[dict] = []
    if image_b64:
        # Strip data: prefix if accidentally included
        if image_b64.startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        # Auto-detect mime from first bytes
        raw = base64.b64decode(image_b64[:20] + "==")  # padding-safe
        mime = "image/jpeg"
        if raw.startswith(b"\x89PNG"):
            mime = "image/png"
        elif raw.startswith(b"GIF"):
            mime = "image/gif"
        elif raw.startswith(b"RIFF") and b"WEBP" in raw:
            mime = "image/webp"
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": image_b64},
        })
    prompt = (text or "").strip() or "Wat staat er op deze foto?"
    user_content.append({"type": "text", "text": f"Joost meldt:\n\"{prompt}\"\n\nSplits uit en log via de tool."})

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[LOG_INTAKE_TOOL],
        tool_choice={"type": "tool", "name": "log_intake"},
        messages=[{"role": "user", "content": user_content}],
    )

    result = _extract_tool_call(response)
    # Ensure all items have confidence field (defensive — tool-schema requires it but be safe)
    for item in result.get("items", []):
        item.setdefault("confidence", "medium")
    return result
