# Output contract

Return one JSON object and nothing else: no Markdown fence, no commentary before or after it.

{"schema_version": "1", "positive_prompt": "Base Block (optics, morphology, 4-point pose, wardrobe, light, core space).\\n\\nDetail Block 1 (fabric physics, styling, materials, environment depth).\\n\\nDetail Block 2 (ambient lighting nuances, micro-textures, legible in-scene text).", "negative_prompt": "", "edit_profile": null, "note": ""}

- `schema_version`: always "1".
- `positive_prompt`: the full English prompt (text, reference and variation modes) or the English edit command (edit mode). Never empty.
  - **Multi-Paragraph Structure (MANDATORY for text, reference, variation):** Structure the prompt into multiple cohesive descriptive paragraphs separated by escaped newlines (`\n\n`):
    1. **Base Block (first ~800 tokens, 130–210 words):** 3–4 cohesive sentences covering optics, character morphology, 4-point pose, wardrobe delta, and key light/space. Must be self-contained.
    2. **Detail Blocks (up to ~2200 tokens):** 1–3 additional paragraphs expanding on textile dynamics, fine anatomy/skin micro-relief, environmental architecture, and legible in-scene text.
    - **CRITICAL:** Do NOT compress or collapse the prompt into a single paragraph or short summary!
- `negative_prompt`: an empty string, except in edit mode with profile `remove`, where it lists the garment or object that must disappear.
- `edit_profile`: in edit mode, one id from the available profiles; in other modes, null.
- `note`: an empty string, or one or two short sentences for the user in the user's language: a long run time, an unreliable removal, a part of the request that was not done, a photo you could not see. No reasoning and no copy of the prompt.
- Escape quotes and line breaks so the object stays valid JSON.
