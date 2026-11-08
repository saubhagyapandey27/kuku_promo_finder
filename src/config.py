"""Configuration: model registry, run profiles, role -> model resolver, price rule."""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"

GENERATOR_ROLES = ("bible", "miner", "composer", "repair", "naive", "auditor")
JUDGE_ROLES = ("hook", "cold", "producer", "jury", "critic")
ALL_ROLES = GENERATOR_ROLES + JUDGE_ROLES

# output budget per call (OpenRouter counts reasoning tokens inside max_tokens, so these are generous)
ROLE_MAX_TOKENS = {"bible": 24000, "miner": 32000, "composer": 16000, "repair": 8000, "naive": 16000,
                   "hook": 12000, "cold": 12000, "producer": 12000, "jury": 10000,
                   "auditor": 6144, "final_viewer": 4096, "critic": 3072}


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except Exception:  # noqa: BLE001
        pass


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    id: str
    provider: str                    # openrouter | gemini | openai_compat | anthropic
    family: str
    price_in: float = 0.0
    price_out: float = 0.0
    price_known: bool = True
    free: bool = False
    moderated: bool = False
    trains: bool = False             # provider may train on prompts (contributor / free tier)
    supports_temperature: bool = True
    effort: str = "medium"           # reasoning effort for this use: minimal|low|medium|high
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    named_exception: bool = False
    free_rpm: Optional[int] = None   # free-tier requests per minute for THIS model (None = provider default)
    free_rpd: Optional[int] = None   # free-tier requests per day for THIS model (None = unknown / unlimited)

    @property
    def label(self) -> str:
        return f"{self.alias}:{self.effort}"

    def with_effort(self, effort: str) -> "ModelSpec":
        return replace(self, effort=effort)


@dataclass
class Profile:
    name: str
    description: str = ""
    roles: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    jury: dict = field(default_factory=lambda: {"seats": 3, "rounds": ["R1", "R2", "R3"], "group_size": 6, "pool": 18})
    pool: dict = field(default_factory=lambda: {"prefilter": 30})
    experiments: dict = field(default_factory=dict)
    allow_same_family: bool = False
    data_collection: Optional[str] = None      # "deny" forces no-training providers for every role
    ranking: dict = field(default_factory=lambda: {"mode":"rubric"})
    custom_endpoint: Optional[dict] = None


def _parse_pref(x: str) -> Tuple[str, str]:
    alias, _, effort = x.partition(":")
    return alias.strip(), (effort.strip() or "medium")


class Config:
    def __init__(self, profile: Profile, models: Dict[str, dict], price_rule: dict, defaults: dict):
        self.profile = profile
        self._models = models
        self.price_rule = price_rule
        self.defaults = defaults

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, profile: str = "quality", config_dir: Optional[Path] = None) -> "Config":
        load_env()
        cdir = Path(config_dir) if config_dir else CONFIG_DIR
        reg = _read_yaml(cdir / "models.yaml")
        p = Path(profile)
        pf = p if p.suffix in (".yaml", ".yml") and p.exists() else cdir / f"{profile}.yaml"
        raw = _read_yaml(pf)
        prof = Profile(
            name=raw.get("name", profile), description=raw.get("description", ""),
            roles={r: [_parse_pref(x) for x in prefs] for r, prefs in (raw.get("roles") or {}).items()},
            ranking=raw.get("ranking") or {"mode":"rubric"},
            jury=raw.get("jury") or Profile.__dataclass_fields__["jury"].default_factory(),
            pool=raw.get("pool") or {"prefilter": 30}, experiments=raw.get("experiments") or {},
            allow_same_family=bool(raw.get("allow_same_family", False)),
            data_collection=raw.get("data_collection"), custom_endpoint=raw.get("custom_endpoint"))
        cfg = cls(prof, reg.get("models", {}), reg.get("price_rule", {}), reg.get("defaults", {}))
        if 'auditor' not in prof.roles:
            prof.roles['auditor'] = list(prof.roles.get('repair', prof.roles.get('miner', [])))
        cfg._apply_env_overrides()
        return cfg

    def _apply_env_overrides(self) -> None:
        for role in ALL_ROLES:
            v = os.environ.get(f"PF_{role.upper()}_MODEL")
            if v:
                self.profile.roles[role] = [_parse_pref(x) for x in v.split(",") if x.strip()]

    # ------------------------------------------------------------------ models
    @property
    def is_custom(self) -> bool:
        return self.profile.custom_endpoint is not None

    def spec(self, alias: str, effort: str = "medium") -> ModelSpec:
        if alias == "custom" and self.profile.custom_endpoint:
            ce = self.profile.custom_endpoint
            mid = os.environ.get(ce.get("model_env", "CUSTOM_MODEL"), "")
            return ModelSpec(alias="custom", id=mid, provider="openai_compat", family="custom",
                             price_in=float(ce.get("price_in", 0)), price_out=float(ce.get("price_out", 0)),
                             price_known=('price_in' in ce and 'price_out' in ce), free=bool(ce.get('free', False)),
                             base_url=os.environ.get(ce.get("base_url_env", "CUSTOM_BASE_URL")),
                             api_key_env=ce.get("api_key_env", "CUSTOM_API_KEY"), effort=effort,
                             supports_temperature=True)
        m = self._models.get(alias)
        if m is None:
            raise KeyError(f"unknown model alias '{alias}' (configs/models.yaml)")
        return ModelSpec(
            alias=alias, id=m["id"], provider=m.get("provider", "openrouter"), family=m.get("family", alias),
            price_in=float(m.get("price_in", 0.0)), price_out=float(m.get("price_out", 0.0)),
            price_known=('price_in' in m and 'price_out' in m),
            free=bool(m.get("free", False)), moderated=bool(m.get("moderated", False)),
            trains=bool(m.get("trains_on_prompts", False)),
            supports_temperature=bool(m.get("supports_temperature", True)), effort=effort,
            base_url=m.get("base_url") or (self.defaults.get("openrouter_base_url") if m.get("provider", "openrouter") == "openrouter" else None),
            api_key_env=m.get("api_key_env"), named_exception=alias in (self.price_rule.get("named_exceptions") or []),
            free_rpm=(m.get("free_quota") or {}).get("rpm"), free_rpd=(m.get("free_quota") or {}).get("rpd"))

    def price_violation(self, spec: ModelSpec) -> Optional[str]:
        """Strict current rule, including custom endpoints and overrides; no historical exceptions."""
        cap = min(0.50, float(self.price_rule.get("max_out_per_m", 0.5)))
        if not all(math.isfinite(p) and p >= 0 for p in (spec.price_in, spec.price_out)):
            return f'{spec.alias}: invalid price'
        if spec.price_out > cap + 1e-9:
            return f"{spec.alias}: output ${spec.price_out}/M exceeds the ${cap}/M rule"
        if spec.free and (spec.price_in or spec.price_out):
            return f'{spec.alias}: free flag conflicts with nonzero price'
        if not spec.free and not spec.price_known:
            return f'{spec.alias}: paid input/output price unknown; declare prices before use'
        return None

    # ------------------------------------------------------------------ roles
    def role_prefs(self, role: str) -> List[ModelSpec]:
        prefs = self.profile.roles.get(role)
        if not prefs:
            raise KeyError(f"profile '{self.profile.name}' has no role '{role}'")
        out: List[ModelSpec] = []
        for alias, effort in prefs:
            s = self.spec(alias, effort)
            bad = self.price_violation(s)
            if bad:
                raise ValueError(f"role '{role}': {bad}")
            if self.profile.data_collection == "deny" and s.trains:
                continue
            out.append(s)
        if not out:
            raise ValueError(f"role '{role}': no model left after the data-collection filter")
        return out

    def generator_family(self, available: Optional[Sequence[str]] = None) -> str:
        chain = self.resolve("miner", None, available)
        return chain[0].family

    def resolve(self, role: str, generator_family: Optional[str], available: Optional[Sequence[str]] = None) -> List[ModelSpec]:
        """Ordered usable models for a role. Judge roles skip the generator's family unless the profile allows
        same-family judging; `available` (aliases that passed preflight) narrows the list when given."""
        chain = self.role_prefs(role)
        if available is not None:
            chain = [s for s in chain if s.alias in available]
        if role in JUDGE_ROLES and generator_family and not self.profile.allow_same_family:
            diff = [s for s in chain if s.family != generator_family]
            chain = diff                                  # an empty list is a hard stop (the caller reports it)
        return chain

    def jury_seats(self, generator_family: Optional[str], available: Optional[Sequence[str]] = None) -> List[ModelSpec]:
        """First `seats` models from the jury list with distinct families (none from the generator's family)."""
        chain = self.resolve("jury", generator_family, available)
        seats: List[ModelSpec] = []
        fams = set()
        for s in chain:
            if s.family in fams and not self.profile.allow_same_family:
                continue
            seats.append(s)
            fams.add(s.family)
            if len(seats) >= int(self.profile.jury.get("seats", 3)):
                break
        return seats

    def critic_seats(self, generator_family=None, available=None) -> List[ModelSpec]:
        """All explicitly configured closure critics; legacy profiles use their jury seats."""
        if 'critic' in self.profile.roles:
            return self.resolve('critic', generator_family, available)
        return self.jury_seats(generator_family, available)

    def max_tokens(self, role: str) -> int:
        return ROLE_MAX_TOKENS.get(role, 8000)

    def active_roles(self):
        return ['bible','miner','hook','cold','critic'] + ([] if self.profile.ranking.get('mode')=='rubric' else ['jury'])

    def required_credentials(self):
        out=set()
        for role in self.active_roles():
            sp=self.role_prefs(role)[0]
            out.add(sp.api_key_env or ('GEMINI_API_KEY' if sp.provider=='gemini' else 'OPENROUTER_API_KEY'))
        return sorted(out)

    def final_contract_problems(self, available=None):
        if not self.profile.experiments.get('final_cuts',True):
            return []
        problems=[]
        try:
            gf=self.generator_family(available)
            cold=self.resolve('cold',gf,available)
            if not cold:
                problems.append('final cuts require a blind cold viewer')
            if not self.resolve('hook',gf,available):
                problems.append('final cuts require an opening assessor')
            if self.profile.ranking.get('mode')!='rubric':
                seats=self.jury_seats(gf,available)
                if len(seats)!=int(self.profile.jury.get('seats',2)):
                    problems.append('preference mode requires all configured jury seats')
            elif 'critic' not in self.profile.roles:
                problems.append('rubric mode requires an explicit source critic')
            configured=self.critic_seats(self.generator_family())
            critics=self.critic_seats(gf,available)
            if not configured or critics!=configured:
                problems.append('final cuts require 100% configured closure critic coverage')
        except (ValueError,KeyError,IndexError) as exc:
            problems.append(str(exc))
        return problems

    def rpm(self, provider: str) -> int:
        return int((self.defaults.get("rpm") or {}).get(provider, 30))

    def gemini_quota(self) -> dict:
        return dict(self.defaults.get("gemini_free_quota") or {"rpm": 10, "rpd": 250})
