"""Allergen remediation dry-runs.

``POST /remediations`` replays a published recipe version against an immutable
rule-set snapshot in memory and searches the requested candidate substitutions
for the cheapest plan that eliminates every ``prohibit`` violation. Nothing is
persisted: recipes, rules, rule sets and evaluation history are untouched, and
no evaluation record is created.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import RemediationRequest
from ..services.evaluation import resolve_rule_set_binding
from ..services.remediation import Substitution, remediate_recipe

router = APIRouter(prefix="/remediations", tags=["remediations"])


@router.post("")
def create_remediation(body: RemediationRequest, db: Session = Depends(get_db)):
    """Find the cheapest substitution plan that clears all prohibit violations.

    The request names the jurisdiction, a published recipe version, an
    immutable rule-set version and at most one distinct substitute per
    ingredient. The recipe is expanded with the existing nested walk; each
    candidate substitution replaces every occurrence of its ingredient code at
    equal mass, and the leaf set is re-evaluated against the bound rule
    snapshot. The winning plan is unique: smallest total substituted mass,
    then lexicographic order of the sorted substitution pairs. An already
    compliant recipe returns the zero-substitution plan; when no subset of the
    candidates clears every violation the response is ``status=no_solution``.
    """
    binding = resolve_rule_set_binding(
        db, body.rule_set_code, body.rule_set_version, body.jurisdiction
    )
    substitutions = [
        Substitution(ingredient=s.ingredient, substitute=s.substitute)
        for s in body.substitutions
    ]
    return remediate_recipe(
        db,
        body.recipe_code,
        body.recipe_version,
        body.jurisdiction,
        binding,
        substitutions,
    )
