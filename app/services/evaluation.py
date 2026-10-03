"""Evaluation engine.

Aggregates allergen masses over the expanded leaf set, converts them to
final-product concentrations, accumulates cross-contact traces, and matches
everything against the applicable rules. All output lists are deterministically
ordered so results are stable and diffable.

Rules come from one of two sources:

- **Legacy**: the published standalone rules of a jurisdiction (``/rules``).
- **Bound**: the immutable snapshot of a published rule-set version
  (``/rule-sets``), which makes the evaluation replayable against the exact
  same rules forever.

The building blocks (``resolve_published_recipe``, ``build_match_rules``,
``score_leaves``) are also reused by the remediation preview service, which
re-scores in-memory substitution scenarios against the same engine so a
preview can never drift from a real evaluation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..errors import api_error
from ..models import Ingredient, Recipe, RecipeVersion, Rule, RuleSet
from ..units import concentration_to_ppm
from .expansion import CycleError, Expansion, expand_root


def _r6(x: float) -> float:
    return round(float(x), 6)


@dataclass(frozen=True)
class RuleSetBinding:
    """A resolved, immutable rule-set snapshot bound to an evaluation."""

    code: str
    version: int
    digest: str
    jurisdiction: str
    rules: list[dict]

    @property
    def reference(self) -> dict:
        return {"code": self.code, "version": self.version, "digest": self.digest}


def resolve_rule_set_binding(
    db: Session, code: str | None, version: int | None, jurisdiction: str
) -> RuleSetBinding | None:
    """Validate an explicit rule-set binding against the database.

    Returns ``None`` when no binding was requested. Missing (code without
    version or vice versa) or unknown/unpublished references raise stable,
    machine-readable errors.
    """
    if code is None and version is None:
        return None
    if code is None or version is None:
        raise api_error(
            400,
            "RULE_SET_BINDING_INCOMPLETE",
            "rule_set_code and rule_set_version must be provided together",
        )
    rs = db.query(RuleSet).filter(RuleSet.code == code).first()
    if rs is None:
        raise api_error(404, "RULE_SET_NOT_FOUND", f"unknown rule set: {code!r}")
    rsv = next((v for v in rs.versions if v.version == version), None)
    if rsv is None:
        raise api_error(
            404,
            "RULE_SET_VERSION_NOT_FOUND",
            f"rule set {code!r} has no published version {version}",
        )
    if rs.jurisdiction != jurisdiction:
        raise api_error(
            422,
            "RULE_SET_JURISDICTION_MISMATCH",
            f"rule set {code!r} belongs to {rs.jurisdiction!r}, "
            f"but the evaluation requests {jurisdiction!r}",
            rule_set_jurisdiction=rs.jurisdiction,
            requested_jurisdiction=jurisdiction,
        )
    return RuleSetBinding(
        code=rs.code,
        version=rsv.version,
        digest=rsv.digest,
        jurisdiction=rs.jurisdiction,
        rules=rsv.rules,
    )


def resolve_published_recipe(
    db: Session, recipe_code: str, recipe_version: int
) -> tuple[Recipe, RecipeVersion]:
    """Fetch a recipe version and require it to be published.

    Raises the same stable errors as ``POST /evaluations`` (unknown recipe /
    version -> 404, draft version -> 422).
    """
    recipe = db.query(Recipe).filter(Recipe.code == recipe_code).first()
    if recipe is None:
        raise api_error(
            404,
            "RECIPE_NOT_FOUND",
            f"unknown recipe: {recipe_code!r}",
            missing=[{"type": "recipe", "code": recipe_code, "reason": "unknown_recipe"}],
        )
    rv = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == recipe_version)
        .first()
    )
    if rv is None:
        raise api_error(
            404,
            "RECIPE_VERSION_NOT_FOUND",
            f"recipe {recipe_code!r} has no version {recipe_version}",
            missing=[{"type": "recipe_version", "code": recipe_code,
                      "version": recipe_version, "reason": "unknown_version"}],
        )
    if rv.status != "published":
        raise api_error(
            422,
            "VERSION_NOT_PUBLISHED",
            f"recipe {recipe_code!r} version {recipe_version} is not published",
        )
    return recipe, rv


def expand_published(db: Session, recipe: Recipe, rv: RecipeVersion) -> Expansion:
    """Expand a published recipe version, surfacing the same stable errors."""
    try:
        expansion = expand_root(db, rv, recipe.code)
    except CycleError as exc:
        raise api_error(422, "RECIPE_CYCLE", "recipe graph contains a cycle", cycle=exc.cycle)

    if expansion.missing:
        raise api_error(
            422,
            "INCOMPLETE_INPUT",
            "evaluation input is incomplete; supply the minimal missing set",
            missing=[m.as_dict() for m in expansion.missing],
        )
    if expansion.yield_g <= 0:
        raise api_error(422, "EMPTY_RECIPE", "recipe total mass is zero")
    return expansion


def build_match_rules(
    db: Session, jurisdiction: str, binding: RuleSetBinding | None
) -> list[dict]:
    """Uniform rule representation for the scoring engine.

    ``{"code", "allergen", "action", "applies_to", "threshold_ppm"}`` —
    already normalized when sourced from a rule-set snapshot.
    """
    if binding is not None:
        return [
            {
                "code": r["code"],
                "allergen": r["allergen"],
                "action": r["action"],
                "applies_to": r["applies_to"],
                "threshold_ppm": _r6(r["threshold_ppm"]),
            }
            for r in sorted(binding.rules, key=lambda r: r["code"])
        ]
    rows = (
        db.query(Rule)
        .filter(Rule.jurisdiction == jurisdiction, Rule.status == "published")
        .order_by(Rule.code)
        .all()
    )
    return [
        {
            "code": r.code,
            "allergen": r.allergen,
            "action": r.action,
            "applies_to": r.applies_to,
            "threshold_ppm": _r6(concentration_to_ppm(r.threshold, r.unit)),
        }
        for r in rows
    ]


def allergen_fraction(content: dict) -> float:
    """Convert an ingredient allergen marker to a 0..1 mass fraction."""
    return concentration_to_ppm(content["concentration"], content["unit"]) / 1e6


def load_ingredients(db: Session, codes: set[str]) -> dict[str, Ingredient]:
    """Load the immutable ingredient rows keyed by code."""
    if not codes:
        return {}
    return {
        i.code: i
        for i in db.query(Ingredient).filter(Ingredient.code.in_(tuple(sorted(codes)))).all()
    }


@dataclass(frozen=True)
class Score:
    """The deterministic outcome of scoring one concrete leaf set."""

    total_mass_g: float
    contains_mass: dict[str, float]
    cross_paths: dict[str, list[dict]]
    obligations: tuple[dict, ...]
    warnings: tuple[dict, ...]
    violations: tuple[dict, ...]
    unregulated_allergens: tuple[str, ...]


def _classify(action: str, entry: dict, obligations: list, warnings: list, violations: list) -> None:
    if action == "declare":
        obligations.append(entry)
    elif action == "warning":
        warnings.append(entry)
    else:  # prohibit
        violations.append(entry)


def score_leaves(
    leaves: list,
    total_g: float,
    ingredients: dict[str, Ingredient],
    match_rules: list[dict],
) -> Score:
    """Aggregate allergens over a concrete leaf set and match the rules.

    ``leaves`` are :class:`~app.services.expansion.Leaf`-like objects carrying
    ``ingredient_code``, ``mass_g`` and ``path``; the code is looked up in
    ``ingredients`` (an ingredient row with ``allergens``/``cross_contact``).
    """
    contains_mass: dict[str, float] = defaultdict(float)
    contains_paths: dict[str, list[dict]] = defaultdict(list)
    cross_paths: dict[str, list[dict]] = defaultdict(list)

    for leaf in leaves:
        ing = ingredients[leaf.ingredient_code]
        for content in ing.allergens:
            allergen_mass = leaf.mass_g * allergen_fraction(content)
            if allergen_mass <= 0:
                continue
            allergen = content["allergen"]
            contains_mass[allergen] += allergen_mass
            contains_paths[allergen].append(
                {
                    "path": leaf.path,
                    "ingredient": leaf.ingredient_code,
                    "ingredient_mass_g": _r6(leaf.mass_g),
                    "allergen_mass_g": _r6(allergen_mass),
                    "contribution_ppm": _r6(allergen_mass / total_g * 1e6),
                }
            )
        for cc in ing.cross_contact:
            cross_paths[cc].append(
                {
                    "path": leaf.path,
                    "ingredient": leaf.ingredient_code,
                    "ingredient_mass_g": _r6(leaf.mass_g),
                }
            )

    # Stable path ordering within each allergen.
    for paths in list(contains_paths.values()) + list(cross_paths.values()):
        paths.sort(key=lambda p: ("/".join(p["path"]), p["ingredient"]))

    obligations: list[dict] = []
    warnings: list[dict] = []
    violations: list[dict] = []
    regulated: set[str] = set()

    for rule in match_rules:
        threshold_ppm = rule["threshold_ppm"]
        if rule["applies_to"] == "contains":
            mass = contains_mass.get(rule["allergen"])
            if mass is None:
                continue
            regulated.add(rule["allergen"])
            conc_ppm = mass / total_g * 1e6
            if conc_ppm < threshold_ppm:
                continue
            entry = {
                "allergen": rule["allergen"],
                "rule_code": rule["code"],
                "action": rule["action"],
                "source": "contains",
                "threshold_ppm": _r6(threshold_ppm),
                "concentration_ppm": _r6(conc_ppm),
                "trigger_paths": contains_paths[rule["allergen"]],
            }
            _classify(rule["action"], entry, obligations, warnings, violations)
        else:  # cross_contact
            paths = cross_paths.get(rule["allergen"])
            if not paths:
                continue
            regulated.add(rule["allergen"])
            entry = {
                "allergen": rule["allergen"],
                "rule_code": rule["code"],
                "action": rule["action"],
                "source": "cross_contact",
                "threshold_ppm": _r6(threshold_ppm),
                "trigger_paths": paths,
            }
            _classify(rule["action"], entry, obligations, warnings, violations)

    sort_key = lambda e: (e["allergen"], e["rule_code"])  # noqa: E731
    obligations.sort(key=sort_key)
    warnings.sort(key=sort_key)
    violations.sort(key=sort_key)

    present = set(contains_mass) | set(cross_paths)
    unregulated = tuple(sorted(present - regulated))

    return Score(
        total_mass_g=total_g,
        contains_mass=dict(contains_mass),
        cross_paths=dict(cross_paths),
        obligations=tuple(obligations),
        warnings=tuple(warnings),
        violations=tuple(violations),
        unregulated_allergens=unregulated,
    )


def serialize_score(score: Score) -> dict:
    """Shape a :class:`Score` into the stable evaluation response payload."""
    total = score.total_mass_g
    return {
        "status": "completed",
        "total_mass_g": _r6(total),
        "allergens": [
            {
                "allergen": a,
                "concentration_ppm": _r6(score.contains_mass[a] / total * 1e6),
                "concentration_percent": _r6(score.contains_mass[a] / total * 100.0),
            }
            for a in sorted(score.contains_mass)
        ],
        "obligations": list(score.obligations),
        "warnings": list(score.warnings),
        "violations": list(score.violations),
        "unregulated_allergens": list(score.unregulated_allergens),
    }


def evaluate_recipe(
    db: Session,
    recipe_code: str,
    recipe_version: int,
    jurisdiction: str,
    binding: RuleSetBinding | None = None,
) -> dict:
    recipe, rv = resolve_published_recipe(db, recipe_code, recipe_version)
    expansion = expand_published(db, recipe, rv)

    ingredients = load_ingredients(
        db, {leaf.ingredient_code for leaf in expansion.leaves}
    )
    match_rules = build_match_rules(db, jurisdiction, binding)
    score = score_leaves(expansion.leaves, expansion.yield_g, ingredients, match_rules)

    result = {
        "recipe": {"code": recipe_code, "version": recipe_version},
        "jurisdiction": jurisdiction,
        "rule_set": binding.reference if binding is not None else None,
        "resolved_recipes": expansion.resolved_recipes,
    }
    result.update(serialize_score(score))
    return result
