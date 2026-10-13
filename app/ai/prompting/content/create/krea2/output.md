# Output contract

Return one JSON object and nothing else: no Markdown fence, no commentary before or after it.

{"schema_version": "1", "positive_prompt": "...", "negative_prompt": "", "edit_profile": null, "note": ""}

- `schema_version`: always "1".
- `positive_prompt`: the full English description (text, reference and variation modes) or the English edit command (edit mode). Never empty.
- `negative_prompt`: an empty string, except in edit mode with profile `remove`, where it lists the garment or object that must disappear.
- `edit_profile`: in edit mode, one id from the available profiles; in other modes, null.
- `note`: an empty string, or one or two short sentences for the user in the user's language: a long run time, an unreliable removal, a part of the request that was not done, a photo you could not see. No reasoning and no copy of the prompt.
- Escape quotes and line breaks so the object stays valid JSON.
