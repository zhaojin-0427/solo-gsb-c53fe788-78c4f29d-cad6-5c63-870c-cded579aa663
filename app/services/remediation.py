"""Allergen remediation preview ("what can we substitute to pass prohibit?").

Given a *published* recipe version, a jurisdiction and an *immutable rule-set
snapshot*, the client proposes at most one alternative ingredient per
ingredient code. The service:

1. Expands the recipe through the same recursive engine as a real evaluation
   (unit conversion, sub-recipe scaling, cycle / missing-reference detection).
2. In memory, replaces **every** leaf occurrence of a mapped ingredient code
   with its alternative ingredient at **equal mass** (the batch yield and total
   mass never change).
3. Re-runs the identical scoring engine against the bound rule snapshot.

The search is exhaustive over the powerset of the proposed mappings (the set
of distinct leaf ingredients in a recipe is small), selecting the unique plan
that removes every ``prohibit`` violation with the smallest replaced mass;
ties are broken by the lexicographic order of the sorted ``(from, to)`` code
pairs. When the original recipe already passes, the unique plan is the empty
plan. Nothing is ever written: recipes, rule sets and evaluation history are
untouched.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from ..errors import api_error
from ..models import Ingredient
from ..schemas import RemediationRequest, RemediationSubstitution
from .evaluation import (
    RuleSetBinding,
    Score,
    allergen_fraction,
    build_match_rules,
    expand_published,
    load_ingredients,
    resolve_published_recipe,
    resolve_rule_set_binding,
    score_leaves,
    serialize_score,
)
from .expansion import Leaf


@dataclass(frozen=True)
class Candidate:
    """One validated equal-mass substitution that may be applied."""

    from_code: str
    to_code: str
    replaced_mass_g: float
    # "may contain" markers introduced at the swapped leaves
    to_cross_contact_set: frozenset[str]
    # allergen -> mass fraction of the alternative ingredient
    to_fractions: dict[str, float]


def validate_substitutions(
    substitutions: list[RemediationSubstitution],
) -> list[tuple[str, str]]:
    """Validate the raw mapping list, preserving input semantics.

    - the same ingredient may be mapped at most once (a repeated source,
      whether or not the target is identical, is a duplicate);
    - an ingredient may not be mapped to itself.

    Returns the (from, to) pairs as normalized codes. Raises stable 422
    errors with the offending, stably-sorted codes.
    """
    pairs = [(s.ingredient, s.alternative) for s in substitutions]
    seen_sources: set[str] = set()
    duplicates: set[str] = set()
    for from_code, _ in pairs:
        if from_code in seen_sources:
            duplicates.add(from_code)
        seen_sources.add(from_code)
    if duplicates:
        raise api_error(
            422,
            "DUPLICATE_SUBSTITUTION",
            "each ingredient may have at most one alternative ingredient",
            ingredients=sorted(duplicates),
        )
    same = sorted({from_code for from_code, to_code in pairs if from_code == to_code})
    if same:
        raise api_error(
            422,
            "SAME_SUBSTITUTION",
            "an ingredient cannot be substituted by itself",
            ingredients=same,
        )
    return pairs


def _cross_contact_set(ingredient: Ingredient) -> frozenset[str]:
    return frozenset(ingredient.cross_contact)


def _mass_by_code(leaves: list[Leaf]) -> dict[str, float]:
    """Total used mass (grams) per leaf ingredient code."""
    used: dict[str, float] = defaultdict(float)
    for leaf in leaves:
        used[leaf.ingredient_code] += leaf.mass_g
    return used


def plan_remediation(
    db: Session, body: RemediationRequest
) -> dict:
    # Bindings and recipe references fail with the same stable responses that
    # evaluations return; the rule-set snapshot is mandatory for remediation.
    binding = resolve_rule_set_binding(
        db, body.rule_set_code, body.rule_set_version, body.jurisdiction
    )
    recipe, rv = resolve_published_recipe(db, body.recipe_code, body.recipe_version)
    expansion = expand_published(db, recipe, rv)

    pairs = validate_substitutions(body.substitutions)

    present_codes = {leaf.ingredient_code for leaf in expansion.leaves}
    to_codes = {to_code for _, to_code in pairs}
    needed = present_codes | to_codes
    ingredients = load_ingredients(db, needed)

    unknown_alternatives = sorted(to_codes - set(ingredients))
    if unknown_alternatives:
        raise api_error(
            422,
            "UNKNOWN_ALTERNATIVE_INGREDIENT",
            "an alternative ingredient is not registered",
            ingredients=unknown_alternatives,
        )
    absent_sources = sorted(
        {from_code for from_code, _ in pairs if from_code not in present_codes}
    )
    if absent_sources:
        raise api_error(
            422,
            "SUBSTITUTION_SOURCE_NOT_USED",
            "the ingredient to replace does not occur in the recipe",
            ingredients=absent_sources,
        )

    match_rules = build_match_rules(db, body.jurisdiction, binding)
    before = score_leaves(
        expansion.leaves, expansion.yield_g, ingredients, match_rules
    )

    used_mass = _mass_by_code(expansion.leaves)
    total_g = expansion.yield_g

    # Per-code allergen fractions of the ORIGINAL ingredients, aggregated from
    # the same ingredient rows the scoring engine uses.
    code_fractions: dict[str, dict[str, float]] = {}
    for code in present_codes:
        ing = ingredients[code]
        code_fractions[code] = {
            c["allergen"]: allergen_fraction(c) for c in ing.allergens
        }
    # Leaves (not mass) carrying each cross-contact marker, per source code.
    code_cross: dict[str, frozenset[str]] = {
        code: _cross_contact_set(ingredients[code]) for code in present_codes
    }

    # Precompute each candidate's equal-mass replacement. Feasibility is later
    # recomputed from totals (never from float subtractions) so exact-threshold
    # decisions match the authoritative re-score.
    candidates: list[Candidate] = []
    for from_code, to_code in pairs:
        to_ing = ingredients[to_code]
        replaced = used_mass[from_code]
        candidates.append(
            Candidate(
                from_code=from_code,
                to_code=to_code,
                replaced_mass_g=round(replaced, 6),
                to_cross_contact_set=_cross_contact_set(to_ing),
                to_fractions={
                    c["allergen"]: allergen_fraction(c) for c in to_ing.allergens
                },
            )
        )
    # Stable enumeration order: sources by code; the tie-break compares the
    # sorted (from, to) pairs, which is exactly the selected-subset order.
    candidates.sort(key=lambda c: (c.from_code, c.to_code))

    prohibit_contains = [
        (r["allergen"], r["threshold_ppm"])
        for r in match_rules
        if r["action"] == "prohibit" and r["applies_to"] == "contains"
    ]
    prohibit_cross = [
        r["allergen"]
        for r in match_rules
        if r["action"] == "prohibit" and r["applies_to"] == "cross_contact"
    ]

    def _feasible(replacement: dict[str, Candidate]) -> bool:
        """Re-score prohibit rules on the mapped scenario.

        The arithmetic deliberately mirrors :func:`score_leaves`: leaves are
        walked in expansion order, each product ``leaf_mass * fraction`` is
        added separately and the concentration is compared with the same
        ``>= threshold`` boundary rule. That makes the preview's decision and
        the returned ``after`` score agree even at an exact ppm boundary.
        Cross-contact prohibit rules trigger per contributing leaf likewise.
        """
        for allergen, threshold_ppm in prohibit_contains:
            mass = 0.0
            for leaf in expansion.leaves:
                cand = replacement.get(leaf.ingredient_code)
                if cand is not None:
                    fraction = cand.to_fractions.get(allergen)
                else:
                    fraction = code_fractions[leaf.ingredient_code].get(allergen)
                if fraction:
                    mass += leaf.mass_g * fraction
            if mass > 0 and mass / total_g * 1e6 >= threshold_ppm:
                return False
        for allergen in prohibit_cross:
            for leaf in expansion.leaves:
                cand = replacement.get(leaf.ingredient_code)
                markers = (
                    cand.to_cross_contact_set
                    if cand is not None
                    else code_cross[leaf.ingredient_code]
                )
                if allergen in markers:
                    return False
        return True

    # Already compliant: the empty plan is the unique minimum and needs no
    # search (every non-empty plan replaces more mass at the same feasibility).
    if not any(v["action"] == "prohibit" for v in before.violations):
        selected: list[int] = []
        status = "already_compliant"
    else:
        # Exhaustive subset search with the spec's total order. Candidates are
        # sorted by source code, so the sorted (from, to) tuple of a selected
        # subset is exactly the pair chain of its indices; comparison is then
        # plain tuple lexicographic order. Among feasible subsets the unique
        # winner minimizes replaced mass, breaking ties on that tuple. Feasibility
        # is recomputed directly from totals at each leaf (the candidate set of
        # distinct leaf ingredients in a recipe is small), while branches whose
        # mass already overshoots the incumbent are pruned.
        n = len(candidates)
        best_mass: float | None = None
        best_key: tuple[tuple[str, str], ...] | None = None

        def search(
            i: int,
            replacement: dict[str, Candidate],
            mass: float,
            chain: list[tuple[str, str]],
        ) -> None:
            nonlocal best_mass, best_key
            m = round(mass, 6)
            if best_mass is not None and m > best_mass:
                return
            if i == n:
                if _feasible(replacement):
                    key = tuple(chain)
                    if best_mass is None or (m, key) < (best_mass, best_key):
                        best_mass = m
                        best_key = key
                return
            cand = candidates[i]
            # Branch A: leave this ingredient untouched first.
            search(i + 1, replacement, mass, chain)
            # Branch B: apply the equal-mass swap at every occurrence.
            replacement[cand.from_code] = cand
            chain.append((cand.from_code, cand.to_code))
            search(i + 1, replacement, mass + cand.replaced_mass_g, chain)
            chain.pop()
            del replacement[cand.from_code]

        search(0, {}, 0.0, [])
        if best_key is None:
            return _build_response(
                body=body,
                binding=binding,
                expansion=expansion,
                before=before,
                status="no_solution",
                selected=[],
                candidates=candidates,
            )
        chosen_set = set(best_key)
        selected = [
            i
            for i, cand in enumerate(candidates)
            if (cand.from_code, cand.to_code) in chosen_set
        ]
        status = "remediated"

    return _build_response(
        body=body,
        binding=binding,
        expansion=expansion,
        before=before,
        status=status,
        selected=selected,
        candidates=candidates,
        ingredients=ingredients,
        match_rules=match_rules,
    )


def _build_response(
    *,
    body: RemediationRequest,
    binding: RuleSetBinding,
    expansion,
    before: Score,
    status: str,
    selected: list[int],
    candidates: list[Candidate],
    ingredients: dict[str, Ingredient] | None = None,
    match_rules: list[dict] | None = None,
) -> dict:
    chosen = [candidates[i] for i in selected]
    replacement = {c.from_code: c.to_code for c in chosen}

    reference = {
        "recipe": {"code": body.recipe_code, "version": body.recipe_version},
        "jurisdiction": body.jurisdiction,
        "rule_set": binding.reference,
    }
    before_payload = {**reference, **serialize_score(before)}

    if status == "no_solution":
        plan = None
        replaced_mass = 0.0
        after_payload = None
    else:
        # Apply the chosen mapping to every leaf occurrence, equal mass.
        effective_leaves = [
            (
                replace(
                    leaf,
                    ingredient_code=replacement[leaf.ingredient_code],
                    path=leaf.path[:-1] + [replacement[leaf.ingredient_code]],
                )
                if leaf.ingredient_code in replacement
                else leaf
            )
            for leaf in expansion.leaves
        ]
        after = score_leaves(
            effective_leaves,
            expansion.yield_g,
            ingredients,
            match_rules,
        )
        after_payload = {**reference, **serialize_score(after)}
        plan = [
            {
                "ingredient": c.from_code,
                "alternative": c.to_code,
                "replaced_mass_g": c.replaced_mass_g,
            }
            for c in chosen
        ]
        replaced_mass = round(sum(c.replaced_mass_g for c in chosen), 6)

    return {
        **reference,
        "resolved_recipes": expansion.resolved_recipes,
        "status": status,
        "plan": plan,
        "replaced_total_mass_g": replaced_mass,
        "before": before_payload,
        "after": after_payload,
    }
