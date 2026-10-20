from . import UNTRUSTED_INPUT_RULE

GRADE_SYSTEM = f"""You judge how related each candidate ticket is to the ticket being worked on.
{UNTRUSTED_INPUT_RULE}

For every candidate return:
- relevance: "relevant" (same problem, or the same cause on the same component),
  "partial" (same component or symptom family but likely a different problem),
  "irrelevant" (different problem).
- score: 0.0-1.0, how confident you are that it is the same problem.
- relation: for OPEN candidates only — "duplicate" (same issue happening right now),
  "possibly_related" (may share a cause), or "unrelated". Use "unrelated" for closed ones.
- reason: one short line.

relevance and relation are separate fields. relevance is only ever "relevant", "partial" or
"irrelevant", even for an open duplicate; "duplicate" goes in relation, never in relevance.

Be strict. A shared product name is not the same problem. Grade every candidate given.
Answer with JSON only."""

GRADE_PROMPT = """<ticket>
Summary: {summary}
Symptoms: {symptoms}
Error codes: {error_codes}
Component: {component}
</ticket>

<candidates>
{candidates}
</candidates>

Grade every candidate."""

CANDIDATE_BLOCK = """<candidate number="{number}" status="{status}" table="{table}">
Category: {category} | Component: {ci} | Group: {group}{extra}
{body}
</candidate>"""
