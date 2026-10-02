"""Allergen remediation dry-run.

Given a published recipe version, an immutable rule-set snapshot and a list of
candidate substitutions (at most one distinct substitute per ingredient), find
the cheapest in-memory substitution plan that eliminates every ``prohibit``
violation:

- leaves come from the existing recursive expansion, so a substitution
  replaces *all* occurrences of an ingredient code at equal mass;
- candidate plans are subsets of the requested substitutions, ordered by
  total substituted mass and then by the lexicographic order of their sorted
  ``(ingredient, substitute)`` pairs, which makes the winning plan unique;
- an already-compliant recipe yields the zero-substitution plan;
- when no subset clears every violation the answer is ``no_solution``.

The dry-run is read-only: recipes, rules, rule sets and evaluation history are
never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from sqlalchemy.orm import Session

from ..errors import api_error
from .evaluation import (
    RuleSetBinding,
    build_match_rules,
    evaluate_leaf_set,
    expand_or_raise,
    get_published_recipe_version,
    load_ingredients,
)
from .expansion import Leaf


def _r6(x: float) -> float:
    return round(float(x), 6)


@dataclass(frozen=True)
class Substitution:
    """A candidate replacement of every occurrence of one ingredient."""

    ingredient: str
    substitute: str


def _validate_substitutions(
    db: Session, substitutions: list[Substitution], leaf_codes: set[str]
) -> None:
    """Reject malformed mappings with stable 422 errors, in a fixed order."""
    seen: set[str] = set()
    for sub in substitutions:
        if sub.ingredient in seen:
            raise api_error(
                422,
                "DUPLICATE_SUBSTITUTION",
                f"ingredient {sub.ingredient!r} has more than one substitution",
                ingredient=sub.ingredient,
            )
        seen.add(sub.ingredient)
    for sub in substitutions:
        if sub.ingredient == sub.substitute:
            raise api_error(
                422,
                "IDENTICAL_SUBSTITUTION",
                f"substitute for {sub.ingredient!r} must be a different ingredient",
                ingredient=sub.ingredient,
            )
    known = load_ingredients(db, [s.substitute for s in substitutions])
    for sub in substitutions:
        if sub.substitute not in known:
            raise api_error(
                422,
                "UNKNOWN_SUBSTITUTE",
                f"unknown substitute ingredient: {sub.substitute!r}",
                substitute=sub.substitute,
            )
    for sub in substitutions:
        if sub.ingredient not in leaf_codes:
            raise api_error(
                422,
                "INGREDIENT_NOT_IN_RECIPE",
                f"ingredient {sub.ingredient!r} does not occur in the recipe",
                ingredient=sub.ingredient,
            )


def remediate_recipe(
    db: Session,
    recipe_code: str,
    recipe_version: int,
    jurisdiction: str,
    binding: RuleSetBinding,
    substitutions: list[Substitution],
) -> dict:
    rv = get_published_recipe_version(db, recipe_code, recipe_version)
    expansion = expand_or_raise(db, rv, recipe_code)

    leaf_codes = {leaf.ingredient_code for leaf in expansion.leaves}
    _validate_substitutions(db, substitutions, leaf_codes)

    mass_by_code: dict[str, float] = {}
    for leaf in expansion.leaves:
        mass_by_code[leaf.ingredient_code] = (
            mass_by_code.get(leaf.ingredient_code, 0.0) + leaf.mass_g
        )

    ingredients = load_ingredients(
        db, leaf_codes | {s.substitute for s in substitutions}
    )
    match_rules = build_match_rules(db, jurisdiction, binding)

    def evaluate_with(mapping: dict[str, str]) -> dict:
        leaves = [
            Leaf(
                mapping.get(leaf.ingredient_code, leaf.ingredient_code),
                leaf.mass_g,
                leaf.path
                if leaf.ingredient_code not in mapping
                else leaf.path[:-1] + [mapping[leaf.ingredient_code]],
            )
            for leaf in expansion.leaves
        ]
        return evaluate_leaf_set(
            leaves=leaves,
            yield_g=expansion.yield_g,
            ingredients=ingredients,
            match_rules=match_rules,
            recipe_code=recipe_code,
            recipe_version=recipe_version,
            jurisdiction=jurisdiction,
            binding=binding,
            resolved_recipes=expansion.resolved_recipes,
        )

    def plan_for(applied) -> dict:
        pairs = sorted((s.ingredient, s.substitute) for s in applied)
        return {
            "substitutions": [
                {
                    "ingredient": ingredient,
                    "substitute": substitute,
                    "substituted_mass_g": _r6(mass_by_code[ingredient]),
                }
                for ingredient, substitute in pairs
            ],
            "substituted_mass_g": _r6(
                sum(mass_by_code[ingredient] for ingredient, _ in pairs)
            ),
        }

    before = evaluate_with({})
    context = {
        "recipe": {"code": recipe_code, "version": recipe_version},
        "jurisdiction": jurisdiction,
        "rule_set": binding.reference,
    }

    if not before["violations"]:
        # Already compliant: the zero-substitution plan.
        return {
            **context,
            "status": "compliant",
            "plan": plan_for([]),
            "before": before,
            "after": before,
        }

    # Every non-empty subset of the candidate substitutions, ordered by
    # (total substituted mass, sorted substitution pairs); the first subset
    # that clears all prohibit violations is the unique cheapest plan.
    candidates = []
    for size in range(1, len(substitutions) + 1):
        for combo in combinations(substitutions, size):
            mass = sum(mass_by_code[s.ingredient] for s in combo)
            pairs = tuple(sorted((s.ingredient, s.substitute) for s in combo))
            candidates.append((mass, pairs, combo))
    candidates.sort(key=lambda c: (c[0], c[1]))

    for _, _, combo in candidates:
        after = evaluate_with({s.ingredient: s.substitute for s in combo})
        if not after["violations"]:
            return {
                **context,
                "status": "remediated",
                "plan": plan_for(combo),
                "before": before,
                "after": after,
            }

    return {
        **context,
        "status": "no_solution",
        "plan": None,
        "before": before,
        "after": None,
    }
