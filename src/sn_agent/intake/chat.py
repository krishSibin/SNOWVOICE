"""The text channel: a streaming conversation over the TCS GenAI Lab gateway,
with the session's tools. Used by the terminal (`sn-agent intake`) and by the
web chatbot page when it is not connected to LiveKit.

Replies stream token by token, so the first words appear while the rest is
still being written.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from ..config import Settings
from .prompts import CONVERSATION_STYLE_TEXT, CONVERSATION_SYSTEM, GREETING
from .session import TOOLS, IntakeSession

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
SEARCH_FILLER = "Let me check how this was fixed before. "


def chat_client(settings: Settings):
    import httpx
    from openai import AsyncOpenAI

    from ..tls import ssl_context

    return AsyncOpenAI(
        base_url=settings.tcs_genai_base_url,
        api_key=settings.tcs_genai_api_key.get_secret_value(),
        http_client=httpx.AsyncClient(verify=ssl_context(settings.tcs_genai_verify_ssl), timeout=60),
        max_retries=2,
    )


def chat_model(settings: Settings) -> str:
    return settings.intake_chat_model or settings.model_lite


class TextChat:
    def __init__(self, session: IntakeSession, client: Any, model: str, *, style: str = CONVERSATION_STYLE_TEXT):
        self.session = session
        self.client = client
        self.model = model
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{CONVERSATION_SYSTEM}\n\n{style}"},
            {"role": "assistant", "content": GREETING},
        ]
        session.add_message("assistant", GREETING)

    @property
    def greeting(self) -> str:
        return GREETING

    async def reply(self, user_text: str) -> AsyncIterator[str]:
        """Streams the assistant's reply to one user message."""

        started = time.time()
        first_token: float | None = None
        self.messages.append({"role": "user", "content": user_text})
        self.session.add_message("user", user_text, channel="text")
        spoken: list[str] = []

        for _round in range(MAX_TOOL_ROUNDS + 1):
            text_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=TOOLS,
                stream=True,
                temperature=0.3,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    if first_token is None:
                        first_token = time.time() - started
                    content = delta.content
                    if not text_parts and spoken and spoken[-1] and not spoken[-1][-1].isspace():
                        # A new round after a tool call: keep it from running into the last sentence.
                        content = " " + content.lstrip()
                    text_parts.append(content)
                    yield content
                for call in delta.tool_calls or []:
                    slot = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                    if call.id:
                        slot["id"] = call.id
                    if call.function and call.function.name:
                        slot["name"] += call.function.name
                    if call.function and call.function.arguments:
                        slot["arguments"] += call.function.arguments

            text = "".join(text_parts)
            spoken.append(text)
            if not calls:
                self.messages.append({"role": "assistant", "content": text})
                break

            self.messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                        for c in calls.values()
                    ],
                }
            )
            for call in calls.values():
                if call["name"] == "search_past_fixes" and not "".join(spoken).strip():
                    # The search takes a few seconds; never leave the user looking at nothing.
                    if first_token is None:
                        first_token = time.time() - started
                    spoken.append(SEARCH_FILLER)
                    yield SEARCH_FILLER
                log.info("tool call %s %s", call["name"], call["arguments"])
                result = await self.session.call_tool(call["name"], call["arguments"])
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        else:
            log.warning("stopped after %d tool rounds", MAX_TOOL_ROUNDS)

        reply = "".join(spoken).strip()
        self.session.add_message("assistant", reply)
        self.session.record_turn_latency(first_token if first_token is not None else time.time() - started, time.time() - started)

