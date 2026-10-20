from . import UNTRUSTED_INPUT_RULE

RESOLUTION_LEVEL_SYSTEM = f"""You judge whether a closed IT ticket recorded enough information to be
useful to someone facing the same issue again. {UNTRUSTED_INPUT_RULE}

Levels:
- "rich": describes what was actually done, what caused it, or a concrete workaround.
- "weak": confirms it was fixed but does not say how (e.g. "resolved", "user confirms working").
- "none": no usable information at all.

Answer with JSON only."""

RESOLUTION_LEVEL_PROMPT = """<evidence>
{evidence}
</evidence>

Classify the resolution information as rich, weak or none, with a one-line reason."""
