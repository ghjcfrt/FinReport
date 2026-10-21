"""Authenticated prompt-profile selection without trusting saved prompt text.

Only the two observed obligation-task base versions have a historical alias.
Policy suffixes are rebuilt by the caller from its validated saved policies.
Other workflow versions retain their exact reconstructed prompt contract.
"""
from __future__ import annotations

import hashlib
from typing import Any

PROFILE_VERSION = "agent_prompt_profile_v1"
TASK_CURRENT = "obligation_task_base_v2"
TASK_LEGACY = "obligation_task_base_v1"
WORKFLOW_DERIVED = "workflow_versioned_base"
CURRENT_PAYLOAD_PROFILE = "obligation_task_compact_v1"
LEGACY_PAYLOAD_PROFILE = "obligation_task_full_v1_unavailable"
TASK_BASE_SHA256 = {
    TASK_LEGACY: "33c9ce77e8c4c5852a6499e8c1dc7c36ece6c72c47884c0527ddb5b071bd6b76",
    TASK_CURRENT: "b8ef48053eb58609170d5859232f3aba8605b577dc34ee7b7b2055e8c3d318bf",
}
_V2_ADDITION_START = "本独立任务协议优先于上文旧逐行协议："


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trusted_task_bases(current: str) -> dict[str, str]:
    if _sha(current) != TASK_BASE_SHA256[TASK_CURRENT]:
        raise ValueError("AGENT_PROMPT_REGISTRY_DRIFT")
    legacy, marker, _addition = current.partition(_V2_ADDITION_START)
    if not marker or _sha(legacy) != TASK_BASE_SHA256[TASK_LEGACY]:
        raise ValueError("AGENT_PROMPT_REGISTRY_DRIFT")
    return {TASK_CURRENT: current, TASK_LEGACY: legacy}


def new_prompt_profile(base: str, suffix: str, *, task_policy: bool) -> tuple[str, dict[str, str]]:
    """Record a new run's selected version and complete authenticated prompt hash."""
    name = TASK_CURRENT if task_policy else WORKFLOW_DERIVED
    if task_policy:
        base = _trusted_task_bases(base)[name]
    system = base + suffix
    return system, {"version": PROFILE_VERSION, "base_profile": name,
                    "payload_profile": CURRENT_PAYLOAD_PROFILE if task_policy else WORKFLOW_DERIVED,
                    "system_sha256": _sha(system)}


def replay_prompt(base: str, suffix: str, saved_system: Any, profile: Any, *,
                  task_policy: bool) -> str:
    """Select only trusted reconstructions; unknown saved prompt strings fail closed."""
    if not isinstance(saved_system, str):
        raise TypeError("REPLAY_AGENT_REQUEST_MISMATCH")
    bases = _trusted_task_bases(base) if task_policy else {WORKFLOW_DERIVED: base}
    if profile is not None:
        if (not isinstance(profile, dict)
                or set(profile) != {"version", "base_profile", "payload_profile", "system_sha256"}
                or profile.get("version") != PROFILE_VERSION
                or not isinstance(profile.get("base_profile"), str)
                or profile["base_profile"] not in bases):
            raise ValueError("REPLAY_AGENT_PROMPT_PROFILE_MISMATCH")
        expected = bases[profile["base_profile"]] + suffix
        payload_profile = (LEGACY_PAYLOAD_PROFILE if profile["base_profile"] == TASK_LEGACY
                           else CURRENT_PAYLOAD_PROFILE if task_policy else WORKFLOW_DERIVED)
        if profile.get("payload_profile") != payload_profile:
            raise ValueError("REPLAY_AGENT_PROMPT_PROFILE_MISMATCH")
        if profile.get("system_sha256") != _sha(expected) or saved_system != expected:
            raise ValueError("REPLAY_AGENT_PROMPT_PROFILE_MISMATCH")
        return expected
    # Pre-profile traces can select the historical task base only by an exact
    # registered digest AND exact equality to a trusted reconstruction.  A
    # recomputed outer request hash cannot register arbitrary new instructions.
    for registered in bases.values():
        expected = registered + suffix
        if _sha(saved_system) == _sha(expected) and saved_system == expected:
            return expected
    raise ValueError("REPLAY_AGENT_REQUEST_MISMATCH")


def is_registered_legacy_task_prompt(system: str, suffix: str) -> bool:
    """Classify an already reconstructed prompt for an explicit payload boundary."""
    if suffix and not system.endswith(suffix):
        return False
    base = system[:-len(suffix)] if suffix else system
    return _sha(base) == TASK_BASE_SHA256[TASK_LEGACY]
