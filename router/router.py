"""router — ONE module that turns router.yaml into runnable SDK objects.

Why a router exists: this plan bills **requests**, not tokens, and a team run's
cost is decided by two things — which model answers which role, and whether the
prompt cache survives. Those decisions belong in one auditable file, not
scattered across agent constructors.

    from router import Router
    r = Router.load()
    lane = r.lane_for("architect")
    llm  = r.build_llm("architect")
    with r.tracked("architect"):            # request accounting
        ...
    print(r.budget_report())

Rules ENFORCED here rather than documented and hoped for:
  * model prefix and base_url must agree (checked at config-parse time)
  * an unknown role/lane raises — there is no silent default model
  * a lane whose probes are not at least ``verified_in_brief`` cannot serve a
    tool-calling role in a live loop (probe-first, per the brief)
  * request budgets are counted here, because the SDK's USD guard cannot work
    when litellm has no price map for these models
  * an expired lane (Kimi backup) raises instead of quietly spending a lapsed
    subscription
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fix.oh_compat import correct_llm_kwargs, validate_lane  # noqa: E402
from lib.keys import KIMI_BASE_URL  # noqa: E402

DEFAULT_CONFIG = REPO / "router" / "router.yaml"
#: Probe states good enough to put a lane in a live tool-calling loop.
TRUSTED_PROBES = ("verified_here", "verified_in_brief")

__all__ = ["Lane", "Probes", "Router", "RouterError", "DEFAULT_CONFIG"]


class RouterError(RuntimeError):
    """Configuration is unsafe to run. Raised before any API call."""


@dataclass(frozen=True)
class Probes:
    """What has actually been demonstrated about this lane, and by whom."""

    raw_curl: str = "untested"
    litellm: str = "untested"
    sdk_conversation: str = "untested"
    tools_required: bool = True

    @property
    def trusted(self) -> bool:
        """A tool-calling lane needs the endpoint AND the litellm path proven."""
        if not self.tools_required:
            return self.raw_curl in TRUSTED_PROBES
        return (self.raw_curl in TRUSTED_PROBES
                and self.litellm in TRUSTED_PROBES)

    @property
    def weakest(self) -> str:
        for state in (self.raw_curl, self.litellm, self.sdk_conversation):
            if state not in TRUSTED_PROBES:
                return state
        return "verified"


@dataclass(frozen=True)
class Lane:
    name: str
    model: str
    base_url: str
    purpose: str = ""
    temperature: float = 0.0
    max_iterations: int = 25
    max_requests_per_run: int = 40
    max_budget_usd: float | None = None
    concurrency_limit: int = 2
    one_shot_outside_loop: bool = False
    extra_body: dict[str, Any] = field(default_factory=dict)
    capability_overrides: dict[str, Any] = field(default_factory=dict)
    probes: Probes = field(default_factory=Probes)
    expires: str | None = None
    degraded: bool = False

    @property
    def transport(self) -> str:
        return self.model.split("/", 1)[0]

    def expired(self, today: dt.date | None = None) -> bool:
        if not self.expires:
            return False
        try:
            end = dt.date.fromisoformat(str(self.expires))
        except ValueError:
            return False
        return (today or dt.date.today()) > end


class Router:
    """Load router.yaml once; hand out lanes, LLM kwargs, budget accounting."""

    def __init__(self, cfg: dict[str, Any], source: str = "<dict>"):
        self.cfg = cfg
        self.source = source
        self.defaults = dict(cfg.get("defaults") or {})
        self.budget = dict(cfg.get("budget") or {})
        self.cache_rules = dict(cfg.get("prefix_cache") or {})
        self._lanes = self._parse_lanes(cfg.get("lanes") or {})
        self._roles = self._parse_roles(cfg.get("roles") or {})
        self._spent: dict[str, int] = {}
        self._validate_all()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Router":
        import yaml

        cfg_path = Path(path or DEFAULT_CONFIG)
        if not cfg_path.is_file():
            raise RouterError(f"router config not found: {cfg_path}")
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RouterError(f"{cfg_path} did not parse to a mapping")
        return cls(data, source=str(cfg_path))

    # ------------------------------------------------------------ parsing
    def _default(self, key: str, fallback: Any) -> Any:
        return self.defaults.get(key, fallback)

    def _parse_lanes(self, raw: dict) -> dict[str, Lane]:
        lanes: dict[str, Lane] = {}
        for name, spec in raw.items():
            if not isinstance(spec, dict):
                raise RouterError(f"lane {name!r} is not a mapping")
            model = spec.get("model")
            if not model:
                raise RouterError(f"lane {name!r} has no model")
            transport = str(spec.get("transport") or "").strip().lower()
            declared = spec.get("base_url")
            _, expected = validate_lane(model, None)
            if transport == "kimi":
                base = KIMI_BASE_URL
            elif declared:
                # An explicit URL is a claim; check it against the model prefix
                # and refuse the config here rather than meeting an opaque HTTP
                # 400 mid-conversation, far from the line that caused it.
                base = str(declared)
                if base.rstrip("/") != expected.rstrip("/"):
                    raise RouterError(
                        f"lane {name!r}: model {model!r} requires base_url "
                        f"{expected!r} but config says {base!r}")
            else:
                base = expected          # derive from the prefix: always coherent
            probes_raw = spec.get("probes") or {}
            lanes[name] = Lane(
                name=name, model=model, base_url=base,
                purpose=str(spec.get("purpose") or ""),
                temperature=float(spec.get("temperature", self._default("temperature", 0.0))),
                max_iterations=int(spec.get("max_iterations",
                                            self._default("max_iterations", 25))),
                max_requests_per_run=int(spec.get("max_requests_per_run",
                                                  self._default("max_requests_per_run", 40))),
                max_budget_usd=spec.get("max_budget_usd"),
                concurrency_limit=int(spec.get("concurrency_limit",
                                               self._default("concurrency_limit", 2))),
                one_shot_outside_loop=bool(spec.get("one_shot_outside_loop", False)),
                extra_body=dict(spec.get("extra_body") or {}),
                capability_overrides=dict(spec.get("capability_overrides") or {}),
                probes=Probes(
                    raw_curl=str(probes_raw.get("raw_curl", "untested")),
                    litellm=str(probes_raw.get("litellm", "untested")),
                    sdk_conversation=str(probes_raw.get("sdk_conversation", "untested")),
                    tools_required=bool(probes_raw.get("tools_required", True)),
                ),
                expires=spec.get("expires"),
                degraded=bool(spec.get("degraded", False)),
            )
        return lanes

    def _parse_roles(self, raw: dict) -> dict[str, dict[str, Any]]:
        roles: dict[str, dict[str, Any]] = {}
        for name, spec in raw.items():
            if isinstance(spec, str):
                spec = {"lane": spec}
            if not isinstance(spec, dict) or "lane" not in spec:
                raise RouterError(f"role {name!r} must map to a lane")
            if spec["lane"] not in self._lanes:
                raise RouterError(
                    f"role {name!r} points at unknown lane {spec['lane']!r}; "
                    f"known lanes: {sorted(self._lanes)}")
            roles[name] = {"lane": spec["lane"],
                           "tools": list(spec.get("tools") or []),
                           "parallel": bool(spec.get("parallel", False))}
        return roles

    def _validate_all(self) -> None:
        band = self.cache_rules.get("compact_band") or [0.60, 0.70]
        try:
            low, high = float(band[0]), float(band[1])
        except Exception as exc:
            raise RouterError(f"prefix_cache.compact_band malformed: {band!r}") from exc
        if not (0.0 < low <= high < 1.0):
            raise RouterError(f"compact_band out of range: {band!r}")
        self.compact_band = (low, high)
        if not self._lanes:
            raise RouterError("config defines no lanes")
        if not self._roles:
            raise RouterError("config defines no roles")

    # ------------------------------------------------------------ selection
    def lane_for(self, role: str, *, allow_untrusted: bool = False,
                 allow_expired: bool = False) -> Lane:
        """Resolve a role to a lane, refusing configurations that are not proven.

        This is the graceful-degradation lever the brief asks for: when a lane
        cannot be trusted inside a loop, set ``one_shot_outside_loop: true`` and
        document why — do not let it spend quota guessing.
        """
        if role not in self._roles:
            raise RouterError(f"unknown role {role!r}; known roles: {sorted(self._roles)}")
        lane = self._lanes[self._roles[role]["lane"]]
        if lane.expired() and not allow_expired:
            raise RouterError(
                f"lane {lane.name!r} expired {lane.expires}; re-point the role or "
                "pass allow_expired=True")
        if not lane.probes.trusted and not allow_untrusted:
            raise RouterError(
                f"lane {lane.name!r} is not probe-trusted for role {role!r} "
                f"(weakest state: {lane.probes.weakest!r}). Run repro/raw_curl.sh "
                "and repro/ab_probe.py, record the result in router.yaml, or set "
                "one_shot_outside_loop: true and keep it out of the tool loop.")
        return lane

    def tools_for(self, role: str) -> list[str]:
        if role not in self._roles:
            raise RouterError(f"unknown role {role!r}")
        return list(self._roles[role]["tools"])

    def is_parallel(self, role: str) -> bool:
        return bool(self._roles.get(role, {}).get("parallel", False))

    @property
    def roles(self) -> list[str]:
        return sorted(self._roles)

    @property
    def lanes(self) -> list[str]:
        return sorted(self._lanes)

    def table(self) -> list[dict[str, Any]]:
        """The role -> lane -> model view an auditor or HANDOFF.md needs."""
        rows = []
        for role, spec in sorted(self._roles.items()):
            lane = self._lanes[spec["lane"]]
            rows.append({"role": role, "lane": lane.name, "model": lane.model,
                         "transport": lane.transport, "base_url": lane.base_url,
                         "tools": spec["tools"], "parallel": spec["parallel"],
                         "one_shot_outside_loop": lane.one_shot_outside_loop,
                         "max_iterations": lane.max_iterations,
                         "max_requests_per_run": lane.max_requests_per_run,
                         "probes": {"raw_curl": lane.probes.raw_curl,
                                    "litellm": lane.probes.litellm,
                                    "sdk_conversation": lane.probes.sdk_conversation},
                         "trusted": lane.probes.trusted,
                         "expired": lane.expired(),
                         "degraded": lane.degraded})
        return rows

    # ------------------------------------------------------------ building
    def llm_kwargs(self, role: str, *, allow_untrusted: bool = False,
                   allow_expired: bool = False) -> dict[str, Any]:
        lane = self.lane_for(role, allow_untrusted=allow_untrusted,
                             allow_expired=allow_expired)
        kwargs = correct_llm_kwargs(lane.model, temperature=lane.temperature,
                                    capability_overrides=lane.capability_overrides or None)
        if lane.transport == "kimi":
            from lib.keys import kimi_key

            kwargs["base_url"] = KIMI_BASE_URL
            kwargs["api_key"] = kimi_key()
        if lane.extra_body:
            # Forwarded verbatim in the request body; this is how enable_thinking
            # reaches Token Plan models, matching the owner's ~/.qwen config.
            kwargs["litellm_extra_body"] = dict(lane.extra_body)
        return kwargs

    def build_llm(self, role: str, **kwargs):
        from openhands.sdk import LLM

        return LLM(**self.llm_kwargs(role, **kwargs))

    def conversation_kwargs(self, role: str, *, cache_key_salt: str = "") -> dict[str, Any]:
        """Budget + prefix-cache settings for a LocalConversation.

        CAVEAT: ``max_budget_per_run`` and ``prompt_cache_key`` are accepted by
        LocalConversation but NOT by the public ``Conversation`` factory, which
        raises TypeError. Use :meth:`public_conversation_kwargs` with the factory.
        """
        lane = self.lane_for(role)
        out: dict[str, Any] = {"max_iteration_per_run": lane.max_iterations}
        if lane.max_budget_usd is not None:
            out["max_budget_per_run"] = lane.max_budget_usd
        if self.cache_rules.get("per_lane_cache_key", True):
            # Stable, secret-free, and deliberately containing NO timestamp or run
            # id: either would guarantee a cache miss, breaking rule 1 of the
            # prefix-cache contract. Parallel fixers on one lane should share a
            # prefix, not thrash separate shards.
            key = f"mas:{lane.name}"
            if cache_key_salt:
                key = f"{key}:{cache_key_salt}"
            out["prompt_cache_key"] = key
        return out

    def public_conversation_kwargs(self, role: str) -> dict[str, Any]:
        """Only what the public ``Conversation`` factory will accept."""
        return {"max_iteration_per_run": self.lane_for(role).max_iterations}

    # ------------------------------------------------------------ budgeting
    def requests_left(self, lane: Lane) -> int:
        return max(lane.max_requests_per_run - self._spent.get(lane.name, 0), 0)

    @contextlib.contextmanager
    def tracked(self, role: str):
        """Charge one request to the lane budget *before* it is sent.

        Checking before the call is the point: after the call the quota is gone
        and a check only tells you the damage is already done.
        """
        lane = self.lane_for(role)
        if self.requests_left(lane) <= 0:
            raise RouterError(
                f"lane {lane.name!r} has spent its request budget "
                f"({lane.max_requests_per_run}); refusing to send another request")
        self._spent[lane.name] = self._spent.get(lane.name, 0) + 1
        _record(lane.model, f"multiagent-stack:router:{lane.name}")
        yield lane

    def record(self, role: str, calls: int = 1) -> None:
        lane = self.lane_for(role)
        self._spent[lane.name] = self._spent.get(lane.name, 0) + calls
        _record(lane.model, f"multiagent-stack:router:{lane.name}", calls=calls)

    def spent(self) -> dict[str, int]:
        return dict(self._spent)

    def total_requests(self) -> int:
        return sum(self._spent.values())

    def night_window_active(self) -> dict[str, Any]:
        return _night_hint(self.budget.get("night_window") or {})

    def budget_report(self) -> dict[str, Any]:
        ceiling = int(self.budget.get("max_requests_per_team_run", 100))
        used = self.total_requests()
        return {"config": self.source,
                "run_requests_used": used,
                "run_requests_ceiling": ceiling,
                "run_requests_remaining": max(ceiling - used, 0),
                "over_ceiling": used > ceiling,
                "per_lane_spent": dict(self._spent),
                "plan_caps_requests": dict(self.budget.get("caps_requests") or {}),
                "night_window": self.night_window_active(),
                "prefix_cache_rules": self.cache_rules}

    def assert_run_within_ceiling(self) -> None:
        ceiling = int(self.budget.get("max_requests_per_team_run", 100))
        used = self.total_requests()
        if used > ceiling:
            raise RouterError(f"run spent {used} requests; ceiling is {ceiling}")


# -------------------------------------------------------------- helpers
def _record(model: str, source: str, calls: int = 1) -> None:
    """Append to the owner's tokenwatch ledger via lib.ledger (the chokepoint).

    Kept as a thin wrapper so Router stays testable: tests set
    ``MULTIAGENT_LEDGER=off`` and can then exercise ``tracked()`` / ``record()``
    without writing fictional request counts into a real accounting file. That
    pollution happened in this repo's first test pass and is the reason the
    chokepoint exists.
    """
    from lib.ledger import record

    record(model, calls, source)


def _offset_for(tz_label: str) -> dt.timezone:
    """'UTC+8' -> a fixed-offset tzinfo. Falls back to UTC rather than guessing."""
    import re

    match = re.match(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", tz_label or "")
    if not match:
        return dt.timezone.utc
    sign = 1 if match.group(1) == "+" else -1
    return dt.timezone(sign * dt.timedelta(hours=int(match.group(2)),
                                           minutes=int(match.group(3) or 0)))


def _night_hint(window: dict[str, Any],
                now: dt.datetime | None = None) -> dict[str, Any]:
    """Is the discounted window active? A scheduling hint, never a hard block."""
    if not window:
        return {"configured": False}
    tz = _offset_for(str(window.get("tz", "UTC")))
    local = (now or dt.datetime.now(tz)).astimezone(tz)
    start = int(window.get("start_hour", 22))
    end = int(window.get("end_hour", 8))
    if start > end:                      # a window that crosses midnight
        active = local.hour >= start or local.hour < end
    else:
        active = start <= local.hour < end
    return {"configured": True, "tz": window.get("tz"), "local_hour": local.hour,
            "window": f"{start:02d}:00-{end:02d}:00", "active": active,
            "prefer": bool(window.get("prefer", True))}


if __name__ == "__main__":  # zero API calls: config inspection only
    router = Router.load()
    print(f"config : {router.source}")
    print(f"roles  : {router.roles}")
    print(f"lanes  : {router.lanes}\n")
    for row in router.table():
        print(f"  {row['role']:13s} -> {row['lane']:18s} {row['model']:28s} "
              f"tools={row['tools']} trusted={row['trusted']} "
              f"parallel={row['parallel']} expired={row['expired']}")
    print()
    print("architect llm kwargs :",
          {k: ("<REDACTED>" if k == "api_key" else v)
           for k, v in router.llm_kwargs("architect").items()})
    print("architect conv kwargs:", router.conversation_kwargs("architect"))
    print("public conv kwargs    :", router.public_conversation_kwargs("architect"))
    print("night window          :", router.night_window_active())
    print("budget report         :", router.budget_report())

