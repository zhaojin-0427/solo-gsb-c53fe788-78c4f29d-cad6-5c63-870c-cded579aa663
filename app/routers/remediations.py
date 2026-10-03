"""Allergen remediation preview.

``POST /remediations`` proposes equal-mass ingredient substitutions that
remove every ``prohibit`` violation of a published recipe under an immutable
rule-set snapshot. It is a pure preview: nothing is persisted (no recipe, rule
set or evaluation history changes), so it needs no idempotency key and the
ordinary evaluation/idempotent-replay behavior is untouched.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import RemediationRequest
from ..services.remediation import plan_remediation

router = APIRouter(prefix="/remediations", tags=["remediations"])


@router.post("", status_code=200)
def create_remediation(body: RemediationRequest, db: Session = Depends(get_db)):
    """Return the unique minimum-mass substitution plan.

    ``status`` is one of ``remediated`` (a plan removing every prohibit
    violation), ``already_compliant`` (the original recipe already passes;
    the plan is empty) or ``no_solution`` (none of the proposed mappings can
    remove every prohibit violation; ``plan`` and ``after`` are null).
    """
    return plan_remediation(db, body)
