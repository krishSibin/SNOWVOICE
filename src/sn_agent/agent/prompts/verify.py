from . import UNTRUSTED_INPUT_RULE

SUPPORT_SYSTEM = f"""You check whether each claim is actually supported by the evidence it cites.
{UNTRUSTED_INPUT_RULE}

You are not judging whether the claim is a good idea, only whether the cited text says it.
- "supported": the cited evidence states it, or states something that plainly implies it.
- "partial": the evidence points that way but is missing part of the claim.
- "unsupported": the evidence does not show this, or the claim adds specifics that are not there.

Judge every claim you are given, by its claim_id. Answer with JSON only."""

SUPPORT_PROMPT = """<claims>
{claims}
</claims>

Label every claim."""

CLAIM_BLOCK = """claim_id: {claim_id}
claim: {text}
cited evidence:
{evidence}"""
