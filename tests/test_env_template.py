"""The .env.example template must stay usable as-is."""

from __future__ import annotations

import re
from pathlib import Path

from sn_agent.config import Settings

TEMPLATE = Path(__file__).resolve().parents[1] / ".env.example"
SCRIPT_ONLY = {"SN_SEED_USERNAME", "SN_SEED_PASSWORD"}


def _names() -> set[str]:
    return set(re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", TEMPLATE.read_text(encoding="utf-8")))


def test_template_lists_every_setting():
    """A new setting added to Settings but forgotten here would be invisible to
    whoever fills in .env."""

    expected = {name.upper() for name in Settings.model_fields}
    missing = expected - _names()
    assert not missing, f"add these to .env.example: {sorted(missing)}"


def test_template_has_no_unknown_settings():
    unknown = _names() - {name.upper() for name in Settings.model_fields} - SCRIPT_ONLY
    assert not unknown, f"not read by the program: {sorted(unknown)}"


def test_template_holds_no_values():
    """It is committed to git, so it must never carry a real key or password."""

    filled = re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=(\S.*)$", TEMPLATE.read_text(encoding="utf-8"))
    assert filled == []


def test_blank_values_fall_back_to_the_defaults():
    """Copied straight to .env, the template must load, not crash on `MAX_ITERATIONS=`."""

    loaded = Settings(_env_file=str(TEMPLATE))

    assert loaded.max_iterations == 3
    assert loaded.worker_threads == 3
    assert loaded.model_flash == "azure/genailab-maas-gpt-4o"
    assert loaded.servicenow_auth_mode == "basic"
    assert loaded.webhook_enabled is False
