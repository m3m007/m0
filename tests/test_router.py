"""Offline tests for router/router.py + router.yaml (T5 gate). Zero API calls.

The router's job is to make dangerous configurations impossible, so most of
these tests assert that something RAISES. A router that happily builds an LLM
for an unprobed model is how a team run burns 120 requests to discover a 400.
"""

from __future__ import annotations

import copy
import datetime as dt
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib.keys import ANTHROPIC_BASE_URL, COMPATIBLE_BASE_URL  # noqa: E402
from router import Lane, Probes, Router, RouterError  # noqa: E402


@pytest.fixture(scope="module")
def router():
    return Router.load()


# ------------------------------------------------------- config integrity
def test_shipped_config_loads(router):
    assert router.roles and router.lanes
    assert "architect" in router.roles and "fixer" in router.roles


def test_every_role_resolves_to_a_real_lane(router):
    for role in router.roles:
        lane = router.lane_for(role)
        assert lane.model.count("/") == 1, f"{role}: model needs a transport prefix"


def test_every_trusted_lane_prefix_matches_its_endpoint(router):
    for row in router.table():
        model, url = row["model"], row["base_url"]
        if model.startswith("anthropic/"):
            assert url == ANTHROPIC_BASE_URL, row
        elif model.startswith("openai/") and "kimi" not in model:
            assert url == COMPATIBLE_BASE_URL, row


def test_brief_roles_are_all_present(router):
    """Brief deliverable #3: research/architect, implementer, reviewer, +tester."""
    for role in ("architect", "research", "implementer", "reviewer", "tester"):
        assert role in router.roles, f"missing role {role}"


def test_implementer_is_the_flash_model(router):
    assert "flash" in router.lane_for("implementer").model


def test_architect_is_a_frontier_candidate(router):
    """Brief allows glm-5.3 or deepseek-v4-pro once working; glm is verified here."""
    lane = router.lane_for("architect")
    assert "glm-5.3" in lane.model
    assert lane.probes.raw_curl == "verified_here"


# ------------------------------------------------------------ refusals
def test_unknown_role_raises_instead_of_defaulting(router):
    with pytest.raises(RouterError, match="unknown role"):
        router.lane_for("nonexistent-role")


def test_role_pointing_at_missing_lane_is_a_config_error():
    cfg = copy.deepcopy(Router.load().cfg)
    cfg["roles"]["rogue"] = {"lane": "no-such-lane"}
    with pytest.raises(RouterError, match="unknown lane"):
        Router(cfg)


def test_mismatched_prefix_and_url_is_refused_at_parse_time():
    cfg = {"defaults": {}, "lanes": {"bad": {"model": "anthropic/glm-5.3",
                                            "base_url": COMPATIBLE_BASE_URL}},
           "roles": {"bad": {"lane": "bad"}}}
    with pytest.raises(RouterError, match="requires base_url"):
        Router(cfg)


def test_lane_without_model_is_refused():
    with pytest.raises(RouterError, match="no model"):
        Router({"lanes": {"ghost": {}}, "roles": {"ghost": {"lane": "ghost"}}})


def test_empty_config_is_refused():
    with pytest.raises(RouterError):
        Router({"lanes": {}, "roles": {}})


def test_malformed_compact_band_is_refused():
    cfg = copy.deepcopy(Router.load().cfg)
    cfg["prefix_cache"]["compact_band"] = [0.9, 0.1]
    with pytest.raises(RouterError, match="compact_band"):
        Router(cfg)


def test_bad_transport_prefix_raises():
    cfg = {"lanes": {"weird": {"model": "groq/llama"}},
           "roles": {"weird": {"lane": "weird"}}}
    with pytest.raises(ValueError, match="unsupported transport|groq"):
        Router(cfg)


# ------------------------------------------------------ probe-first rule
def test_unprobed_lane_cannot_serve_a_tool_loop():
    cfg = copy.deepcopy(Router.load().cfg)
    cfg["lanes"]["wild"] = {"model": "anthropic/unprobed-9",
                            "probes": {"raw_curl": "untested", "litellm": "untested",
                                       "sdk_conversation": "untested",
                                       "tools_required": True}}
    cfg["roles"]["wild"] = {"lane": "wild"}
    r = Router(cfg)
    with pytest.raises(RouterError, match="not probe-trusted"):
        r.lane_for("wild")
    # the documented escape hatch still works, explicitly
    assert r.lane_for("wild", allow_untrusted=True).model == "anthropic/unprobed-9"


def test_one_shot_lane_needs_only_an_endpoint_probe():
    """Brief: if a lane cannot run in a loop, it may serve one-shot outside it."""
    cfg = copy.deepcopy(Router.load().cfg)
    cfg["lanes"]["oneshot"] = {
        "model": "anthropic/slow-model", "one_shot_outside_loop": True,
        "probes": {"raw_curl": "verified_here", "litellm": "untested",
                   "sdk_conversation": "untested", "tools_required": False}}
    cfg["roles"]["oneshot"] = {"lane": "oneshot"}
    lane = Router(cfg).lane_for("oneshot")
    assert lane.one_shot_outside_loop and lane.probes.trusted


def test_probes_weakest_reports_the_real_gap():
    assert Probes(raw_curl="verified_here", litellm="verified_here",
                  sdk_conversation="untested").weakest == "untested"
    # all three proven -> "verified"
    assert Probes("verified_here", "verified_here", "verified_here").weakest == "verified"
    # default construction leaves everything untested
    assert Probes().weakest == "untested"


# ------------------------------------------------------------ expiry
def test_expired_lane_refuses_by_default():
    cfg = copy.deepcopy(Router.load().cfg)
    cfg["lanes"]["old"] = {"model": "openai/x", "expires": "2020-01-01",
                           "probes": {"raw_curl": "verified_here",
                                      "litellm": "verified_here"}}
    cfg["roles"]["old"] = {"lane": "old"}
    r = Router(cfg)
    with pytest.raises(RouterError, match="expired"):
        r.lane_for("old")
    assert r.lane_for("old", allow_expired=True)


def test_expiry_uses_real_dates():
    lane = Lane(name="l", model="openai/x", base_url=COMPATIBLE_BASE_URL,
                expires="2026-10-10")
    assert not lane.expired(dt.date(2026, 10, 9))
    assert lane.expired(dt.date(2026, 10, 11))
    assert Lane(name="l2", model="openai/x", base_url=COMPATIBLE_BASE_URL).expired() is False


# ------------------------------------------------------------ building
def test_llm_kwargs_are_coherent_and_secret_free(router):
    kwargs = router.llm_kwargs("architect")
    assert kwargs["model"] == "anthropic/glm-5.3"
    assert kwargs["base_url"] == ANTHROPIC_BASE_URL
    assert kwargs["streaming"] is False
    assert kwargs["temperature"] == pytest.approx(0.2)
    # api_key must be a resolved value, never a placeholder string
    assert kwargs["api_key"] and "REDACTED" not in kwargs["api_key"]


def test_extra_body_carries_the_thinking_toggle(router):
    assert router.llm_kwargs("implementer")["litellm_extra_body"] == {"enable_thinking": False}
    assert router.llm_kwargs("reviewer")["litellm_extra_body"] == {"enable_thinking": True}


def test_build_llm_produces_a_working_sdk_object(router):
    from openhands.sdk import LLM

    llm = router.build_llm("architect")
    assert isinstance(llm, LLM)
    assert llm.model == "anthropic/glm-5.3"
    from openhands.sdk.llm.utils.model_features import get_features

    assert get_features(llm.model).thinking_mode in ("none", "unknown", "manual")


def test_conversation_kwargs_carry_iteration_and_cache_key(router):
    ck = router.conversation_kwargs("fixer")
    assert ck["max_iteration_per_run"] == router.lane_for("fixer").max_iterations
    assert ck["prompt_cache_key"] == "mas:implementer"


def test_cache_key_is_stable_and_holds_no_timestamp(router):
    """Rule 1 of the prefix-cache contract: a changing key means a permanent miss."""
    a = router.conversation_kwargs("architect")["prompt_cache_key"]
    b = router.conversation_kwargs("architect")["prompt_cache_key"]
    assert a == b == "mas:architect"
    import re

    assert not re.search(r"\d{6,}", a), "run id or timestamp leaked into cache key"
    salted = router.conversation_kwargs("architect", cache_key_salt="worker-1")
    assert salted["prompt_cache_key"].startswith("mas:architect:")


def test_public_factory_gets_only_supported_kwargs(router):
    """The public Conversation factory rejects max_budget_per_run/prompt_cache_key."""
    from inspect import signature

    from openhands.sdk import Conversation

    public = router.public_conversation_kwargs("architect")
    params = set(signature(Conversation.__new__).parameters)
    for key in public:
        assert key in params, f"{key} is not accepted by the public factory"
    assert "prompt_cache_key" not in public


# ------------------------------------------------------------ budgeting
def test_request_budget_blocks_before_the_call_is_sent(router):
    cfg = copy.deepcopy(router.cfg)
    cfg["lanes"]["architect"]["max_requests_per_run"] = 2
    r = Router(cfg)
    with r.tracked("architect"), r.tracked("architect"):
        pass
    with pytest.raises(RouterError, match="spent its request budget"):
        with r.tracked("architect"):
            pytest.fail("budget exhausted but a request was still allowed")


def test_per_run_ceiling_matches_the_brief_target(router):
    assert router.budget["max_requests_per_team_run"] <= 120
    assert router.budget["max_requests_per_team_run"] >= 50


def test_ceiling_assertion_trips(router):
    cfg = copy.deepcopy(router.cfg)
    cfg["budget"]["max_requests_per_team_run"] = 3
    r = Router(cfg)
    r.record("architect", 4)
    with pytest.raises(RouterError, match="ceiling"):
        r.assert_run_within_ceiling()


def test_budget_report_is_json_friendly_and_secret_free(router):
    import json

    report = router.budget_report()
    text = json.dumps(report, default=str)
    assert router.llm_kwargs("architect")["api_key"] not in text
    assert "run_requests_ceiling" in report


def test_parallel_roles_are_marked(router):
    """Brief deliverable #3 wants concurrency limits + parallel-able roles."""
    assert router.is_parallel("fixer") is True
    assert router.is_parallel("architect") is False
    assert router.lane_for("fixer").concurrency_limit >= 1


def test_tools_are_declared_per_role(router):
    assert "terminal" in router.tools_for("fixer")
    assert "task" in router.tools_for("orchestrator")
    with pytest.raises(RouterError):
        router.tools_for("ghost")


# ------------------------------------------------------- night-window math
def test_night_window_wraps_midnight():
    from router.router import _night_hint

    window = {"tz": "UTC+8", "start_hour": 22, "end_hour": 8, "prefer": True}
    inside = dt.datetime(2026, 10, 5, 23, 30, tzinfo=dt.timezone(
        dt.timedelta(hours=8)))
    outside = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone(
        dt.timedelta(hours=8)))
    assert _night_hint(window, inside)["active"] is True
    assert _night_hint(window, outside)["active"] is False


def test_night_window_non_wrapping_interval():
    from router.router import _night_hint

    window = {"tz": "UTC", "start_hour": 9, "end_hour": 17}
    noon = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
    assert _night_hint(window, noon)["active"] is True
    assert _night_hint(window, dt.datetime(2026, 10, 5, 3, 0,
                                           tzinfo=dt.timezone.utc))["active"] is False
    assert _night_hint({}) == {"configured": False}


def test_unknown_timezone_falls_back_to_utc():
    from router.router import _offset_for

    assert _offset_for("nonsense") is dt.timezone.utc
    assert _offset_for("UTC-5") == dt.timezone(dt.timedelta(hours=-5))


# ------------------------------------------------------------ prefix cache
def test_cache_rules_are_documented_in_config(router):
    rules = router.cache_rules
    assert rules["stable_system_prompt"] and rules["append_only_history"]
    assert rules["frozen_tool_list"]
    low, high = router.compact_band
    assert 0.5 <= low < high <= 0.8, "brief mandates compacting at 60-70%"


def test_table_exposes_probe_provenance_for_auditing(router):
    rows = router.table()
    assert {"probes", "trusted", "one_shot_outside_loop"} <= set(rows[0])
    states = {r["probes"]["raw_curl"] for r in rows}
    assert states <= {"verified_here", "verified_in_brief", "untested", "failed"}
