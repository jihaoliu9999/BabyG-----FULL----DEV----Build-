"""Single source of truth for every prompt used by babyg.

RULE: Every prompt — system prompts, tool descriptions, refusal templates,
Hot Drop personalization templates, scope classifier prompts, persona
moderation prompts, draft email prompts, etc. — lives in this file. Nothing
else in the codebase may contain prompt strings.

Phase 1 Step 1 is scaffold only. Prompt content is added as each feature is
implemented in later phases. Prompts are exposed as module-level constants
or as functions returning a string when context substitution is needed.
"""

# Phase 2: babyg system prompt, scope template, persona moderation prompt
# Phase 2: Central Bot personalization prompt for Hot Drops
# Phase 3: tool-use prompt additions, voice-matching guidance
# Phase 4: DM draft prompt, collab match prompt
# Phase 5: image/PDF analysis prompt for brand briefs
