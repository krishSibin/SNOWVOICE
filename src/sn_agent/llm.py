"""Single place where the TCS GenAI Lab gateway is called. Everything else in
the codebase talks to the `LLMClient` protocol, so the provider can be
swapped without touching the pipeline. Per the AI Fridays handbook, only
models served through https://genailab.tcs.in may be used — no external API
or Model-as-a-Service."""

from __future__ import annotations

import copy
import json
import logging
import random
import time
from enum import Enum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE_STATUS = {408, 500, 502, 503, 504}
RATE_LIMIT_COOLDOWN_S = 60


class Tier(str, Enum):
    LITE = "lite"
    FLASH = "flash"
    PRO = "pro"


class BudgetExceeded(RuntimeError):
    pass


class LLMOutputError(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    """The model could not be reached after retries and fallbacks. The pipeline
    treats this like a spent budget: finish with what we have rather than lose
    the whole run."""


class Meter:
    """Per-ticket token and cost budget. Raises before a call that we already
    know we cannot afford, and after any call that pushes us over."""

    def __init__(self, prices: dict[str, tuple[float, float]], max_cost_usd: float, max_tokens: int):
        self.prices = prices
        self.max_cost_usd = max_cost_usd
        self.max_tokens = max_tokens
        self.cost_usd = 0.0
        self.tokens = 0
        self.calls: list[dict[str, Any]] = []

    @property
    def models_used(self) -> list[str]:
        seen = {c["model"] for c in self.calls}
        return sorted(seen)

    def exhausted(self) -> bool:
        return self.cost_usd >= self.max_cost_usd or self.tokens >= self.max_tokens

    def ensure_available(self, step: str = "") -> None:
        if self.exhausted():
            raise BudgetExceeded(
                f"budget spent before step={step!r}: ${self.cost_usd:.4f} / {self.tokens} tokens"
            )

    def record(self, model: str, step: str, prompt_tokens: int, output_tokens: int) -> None:
        price_in, price_out = self.prices.get(model, (0.0, 0.0))
        cost = (prompt_tokens * price_in + output_tokens * price_out) / 1_000_000
        self.cost_usd += cost
        self.tokens += prompt_tokens + output_tokens
        self.calls.append(
            {
                "model": model,
                "step": step,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost, 6),
            }
        )


class LLMClient(Protocol):
    def generate_json(
        self, *, tier: Tier, system: str, prompt: str, schema: type[T], meter: Meter, step: str
    ) -> T: ...


def inline_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic emits `$ref`/`$defs`; inline them so the schema is a single
    self-contained object we can drop straight into the prompt, and drop keys
    that only matter to a schema validator."""

    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return resolve(copy.deepcopy(defs[name]))
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in {"title", "default", "additionalProperties"}:
                continue
            out[key] = resolve(value)
        return out

    schema = resolve(raw)
    schema.pop("required", None)  # every field has a default; keep the model free to omit
    return schema


class TcsGenAiLLM:
    """Calls the TCS GenAI Lab MaaS gateway, an OpenAI-compatible LiteLLM
    endpoint in front of the models approved for the hackathon (see the AI
    Fridays handbook, Annexure 13.5). `model_lite`/`model_flash`/`model_pro`
    must be one of those gateway model names, e.g.
    `azure/genailab-maas-gpt-4o` or `azure_ai/genailab-maas-DeepSeek-V3-0324`."""

    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        if client is not None:
            self.client = client
        else:
            import httpx
            from openai import OpenAI

            from .tls import ssl_context

            http_client = httpx.Client(verify=ssl_context(settings.tcs_genai_verify_ssl))
            self.client = OpenAI(
                base_url=settings.tcs_genai_base_url,
                api_key=settings.tcs_genai_api_key.get_secret_value(),
                http_client=http_client,
            )
        # Models this key cannot use (retired or not granted). Remembered so a
        # dead model is not retried on every step of every ticket.
        self._retired_models: set[str] = set()
        # Models that just hit their rate limit, and when to try them again.
        self._cooling_until: dict[str, float] = {}

    def model_for(self, tier: Tier) -> str:
        return {
            Tier.LITE: self.settings.model_lite,
            Tier.FLASH: self.settings.model_flash,
            Tier.PRO: self.settings.model_pro,
        }[tier]

    def fallback_models(self, tier: Tier) -> list[str]:
        """Models to try in order. A smaller model answering is better than a
        ticket getting no answer because the big one is overloaded or retired."""

        chain = {
            Tier.PRO: [self.settings.model_pro, self.settings.model_flash, self.settings.model_lite],
            Tier.FLASH: [self.settings.model_flash, self.settings.model_lite],
            Tier.LITE: [self.settings.model_lite, self.settings.model_flash],
        }[tier]
        now = time.time()
        usable = [
            m for m in chain
            if m and m not in self._retired_models and self._cooling_until.get(m, 0) <= now
        ]
        seen: list[str] = []
        for model in usable or chain:
            if model and model not in seen:
                seen.append(model)
        return seen

    def generate_json(
        self, *, tier: Tier, system: str, prompt: str, schema: type[T], meter: Meter, step: str = ""
    ) -> T:
        meter.ensure_available(step)
        candidates = self.fallback_models(tier)
        last_error: Exception | None = None
        for index, model in enumerate(candidates):
            try:
                return self._generate_with(model, system, prompt, schema, meter, step)
            except LLMUnavailable as exc:
                last_error = exc
                if index + 1 < len(candidates):
                    log.warning("%s unavailable for step=%s; falling back to %s", model, step, candidates[index + 1])
        raise LLMUnavailable(f"{step}: no model available ({last_error})")

    def _generate_with(
        self, model: str, system: str, prompt: str, schema: type[T], meter: Meter, step: str
    ) -> T:
        import openai

        system_with_schema = (
            f"{system}\n\n"
            "Respond with a single JSON object only — no markdown fences, no "
            "commentary — matching exactly this JSON Schema:\n"
            f"{json.dumps(inline_json_schema(schema))}"
        )
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            started = time.time()
            try:
                response = self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_with_schema},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                )
            except openai.APIStatusError as exc:
                code = exc.status_code
                if code == 404:
                    # The model is retired or not granted to this key. Retrying
                    # cannot help; another model can.
                    self._retired_models.add(model)
                    log.warning("%s is not available on the gateway; it will not be tried again", model)
                    raise LLMUnavailable(f"{model} is not available on the gateway") from exc
                if code == 429:
                    # A per-minute quota: retrying within seconds almost never
                    # works, so move to the next model now and rest this one.
                    self._cooling_until[model] = time.time() + RATE_LIMIT_COOLDOWN_S
                    log.warning(
                        "%s rate limit reached (429); using another model for the next %ds",
                        model, RATE_LIMIT_COOLDOWN_S,
                    )
                    raise LLMUnavailable(f"{model} rate limited") from exc
                if code not in RETRYABLE_STATUS:
                    log.error("gateway rejected step=%s on %s: %s", step, model, exc)
                    raise
                last_error = exc
                if attempt < self.settings.llm_max_retries:
                    log.warning(
                        "%s is overloaded (%s) on step=%s; retry %d of %d",
                        model, code, step, attempt + 1, self.settings.llm_max_retries,
                    )
                    self._sleep(attempt)
                    continue
                raise LLMUnavailable(f"{model} returned {code} for step={step}") from exc
            except openai.APIConnectionError as exc:
                last_error = exc
                if attempt < self.settings.llm_max_retries:
                    log.warning(
                        "%s unreachable on step=%s; retry %d of %d",
                        model, step, attempt + 1, self.settings.llm_max_retries,
                    )
                    self._sleep(attempt)
                    continue
                raise LLMUnavailable(f"{model} unreachable for step={step}") from exc

            usage = response.usage
            if usage is not None:
                meter.record(
                    model,
                    step,
                    int(usage.prompt_tokens or 0),
                    int(usage.completion_tokens or 0),
                )
            call = meter.calls[-1] if meter.calls else {}
            log.debug(
                "   genai step=%s model=%s %.1fs tokens in=%s out=%s $%.5f",
                step, model, time.time() - started,
                call.get("prompt_tokens", "?"), call.get("output_tokens", "?"), call.get("cost_usd", 0.0),
            )

            text = (response.choices[0].message.content or "").strip()
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                last_error = exc
                first = exc.errors()[0] if exc.errors() else {}
                where = ".".join(str(p) for p in first.get("loc", ()))
                log.warning(
                    "step=%s invalid JSON from %s (attempt %s): %s at %s (got %r), retrying",
                    step, model, attempt, first.get("msg", "invalid"), where or "?",
                    str(first.get("input", ""))[:60],
                )
                if attempt < self.settings.llm_max_retries:
                    meter.ensure_available(step)
                    continue
                raise LLMOutputError(f"{step}: model returned unusable JSON: {text[:400]}") from exc

        raise LLMOutputError(f"{step}: exhausted retries") from last_error

    @staticmethod
    def _sleep(attempt: int) -> None:  # pragma: no cover - timing
        # Overload spikes last seconds to minutes, so back off properly rather
        # than burning the retries in under two seconds.
        time.sleep(min(30.0, 2.0 * 2**attempt) + random.random())
