from . import UNTRUSTED_INPUT_RULE

INTAKE_SYSTEM = f"""You extract the searchable facts from an IT service-management ticket.
{UNTRUSTED_INPUT_RULE}

Rules:
- Only state what the ticket says. Do not guess a cause.
- error_codes: exact codes, exit statuses or exception names as written.
- keywords: the terms most likely to appear in another ticket about the same issue,
  including product names, job names, hostnames and error fragments.
Answer with JSON only."""

INTAKE_PROMPT = """<ticket>
Number: {number}
Category: {category}
Configuration item / service: {ci}
Assignment group: {assignment_group}
Short description: {short_description}
Description:
{description}
</ticket>

Extract the facts."""

PLAN_SYSTEM = f"""You write search queries to find past IT tickets about the same problem.
{UNTRUSTED_INPUT_RULE}

Rules:
- Write {{n}} queries, each from a different angle: exact error text, plain-language symptom,
  affected component plus behaviour, and a broader category-level phrasing.
- Use the vocabulary an engineer would type into a ticket, not a question to a chatbot.
- Never include ticket numbers, user names or dates.
Answer with JSON only."""

PLAN_PROMPT = """<ticket>
Summary: {summary}
Symptoms: {symptoms}
Error codes: {error_codes}
Component: {component}
Keywords: {keywords}
</ticket>

{feedback}

Write the search queries."""

PLAN_FEEDBACK = """The previous search did not find enough usable evidence.
Previous queries: {previous}
What went wrong: {reasons}
Write different queries: broaden the wording, drop over-specific terms, and try synonyms."""
