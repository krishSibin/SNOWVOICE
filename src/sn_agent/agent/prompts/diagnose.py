from . import UNTRUSTED_INPUT_RULE

DIAGNOSE_SYSTEM = f"""An engineer has a ticket with no documented fix in the history. Give them a
useful starting point. {UNTRUSTED_INPUT_RULE}

- gaps: what information is missing that would make this diagnosable.
- diagnostic_questions: concrete checks or questions for the user, ordered by what would
  narrow it down fastest. Be specific to this system and symptom, not generic advice.
- Never state what the cause is or how to fix it. You have no evidence for either.
Answer with JSON only."""

DIAGNOSE_PROMPT = """<ticket>
Summary: {summary}
Symptoms: {symptoms}
Error codes: {error_codes}
Component: {component}
</ticket>

{history}

Give the gaps and the diagnostic questions."""

HISTORY_WITH_UNDOCUMENTED = """Past tickets look like the same issue but none recorded how it was fixed:
{tickets}"""

HISTORY_NONE = "Nothing similar was found in the ticket history."
