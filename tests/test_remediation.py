"""Allergen remediation dry-run API.

Covers plan selection (min substituted mass, lexicographic tie-break),
zero-substitution plans for already-compliant recipes, no-solution outcomes,
substitution validation, reused reference errors and read-only behavior.
"""

from tests.helpers import make_ingredient, make_recipe

RULES = [
    {"code": "gluten-declare", "allergen": "gluten", "threshold": 0,
     "unit": "ppm", "action": "declare", "applies_to": "contains"},
    {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 20,
     "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
    {"code": "tree-nuts-declare", "allergen": "tree_nuts", "threshold": 0,
     "unit": "ppm", "action": "declare", "applies_to": "contains"},
    {"code": "peanut-warning", "allergen": "peanut", "threshold": 0,
     "unit": "ppm", "action": "warning", "applies_to": "cross_contact"},
]


def _publish_set(client, code, rules, jurisdiction="EU"):
    assert client.post(
        "/rule-sets", json={"code": code, "jurisdiction": jurisdiction}
    ).status_code == 201
    assert client.put(
        f"/rule-sets/{code}", json={"base_version": 0, "rules": rules}
    ).status_code == 200
    resp = client.post(f"/rule-sets/{code}/publish", json={"base_version": 1})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _remediate(client, recipe_code="cake", recipe_version=1, jurisdiction="EU",
               rule_set="eu-pack", rule_set_version=1, substitutions=None):
    body = {
        "recipe_code": recipe_code,
        "recipe_version": recipe_version,
        "jurisdiction": jurisdiction,
        "rule_set_code": rule_set,
        "rule_set_version": rule_set_version,
        "substitutions": substitutions or [],
    }
    return client.post("/remediations", json=body)


def _cake_scenario(client):
    """Nested cake: 400 g filling (almond/sugar) + 1 kg wheat flour = 1400 g."""
    make_ingredient(
        client,
        "almond_flour",
        allergens=[{"allergen": "tree_nuts", "concentration": 100.0, "unit": "%"}],
        cross_contact=["peanut"],
    )
    make_ingredient(
        client,
        "wheat_flour",
        allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
    )
    make_ingredient(client, "rice_flour")
    make_ingredient(client, "sugar")
    published = _publish_set(client, "eu-pack", RULES)
    make_recipe(
        client,
        "filling",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "almond_flour",
                     "quantity": 100, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 100, "unit": "g"},
                ],
            }
        ],
    )
    make_recipe(
        client,
        "cake",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "filling", "quantity": 400, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_flour",
                     "quantity": 1, "unit": "kg"},
                ],
            }
        ],
    )
    return published


def test_remediation_replaces_all_occurrences_at_equal_mass(client):
    _cake_scenario(client)
    resp = _remediate(
        client, substitutions=[{"ingredient": "wheat_flour", "substitute": "rice_flour"}]
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()

    assert result["status"] == "remediated"
    assert result["recipe"] == {"code": "cake", "version": 1}
    assert result["jurisdiction"] == "EU"
    assert result["rule_set"]["code"] == "eu-pack"
    assert result["rule_set"]["version"] == 1
    assert result["rule_set"]["digest"]

    # 1 kg of wheat flour replaced by 1 kg of rice flour
    assert result["plan"] == {
        "substitutions": [
            {"ingredient": "wheat_flour", "substitute": "rice_flour",
             "substituted_mass_g": 1000.0}
        ],
        "substituted_mass_g": 1000.0,
    }

    before, after = result["before"], result["after"]
    assert [v["rule_code"] for v in before["violations"]] == ["gluten-prohibit"]
    assert after["violations"] == []
    # gluten is gone entirely: its declare obligation disappears too
    assert [o["allergen"] for o in before["obligations"]] == ["gluten", "tree_nuts"]
    assert [o["allergen"] for o in after["obligations"]] == ["tree_nuts"]
    # cross-contact from the untouched almond flour is preserved
    assert [w["allergen"] for w in after["warnings"]] == ["peanut"]
    # equal-mass replacement keeps the batch mass and nut concentration
    assert after["total_mass_g"] == 1400.0
    assert after["allergens"] == [
        {"allergen": "tree_nuts", "concentration_ppm": 142857.142857,
         "concentration_percent": 14.285714}
    ]


def test_substitution_spans_nested_and_direct_occurrences(client):
    make_ingredient(
        client, "wheat_flour",
        allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
    )
    make_ingredient(client, "rice_flour")
    make_ingredient(client, "sugar")
    _publish_set(client, "eu-pack", RULES)
    make_recipe(
        client,
        "base",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat_flour",
                     "quantity": 60, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 40, "unit": "g"},
                ],
            }
        ],
    )
    make_recipe(
        client,
        "double",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "base", "quantity": 200, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_flour",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 750, "unit": "g"},
                ],
            }
        ],
    )
    resp = _remediate(
        client, recipe_code="double",
        substitutions=[{"ingredient": "wheat_flour", "substitute": "rice_flour"}],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    # 60 g x (200/100) via the sub-recipe + 50 g direct = 170 g
    assert result["plan"]["substituted_mass_g"] == 170.0
    assert result["after"]["violations"] == []
    assert result["after"]["allergens"] == []


def test_already_compliant_returns_zero_substitution_plan(client):
    make_ingredient(client, "sugar")
    _publish_set(client, "eu-pack", RULES)
    make_recipe(
        client,
        "candy",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "sugar", "quantity": 100, "unit": "g"}
                ],
            }
        ],
    )
    resp = _remediate(client, recipe_code="candy")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "compliant"
    assert result["plan"] == {"substitutions": [], "substituted_mass_g": 0.0}
    assert result["before"]["violations"] == []
    assert result["after"] == result["before"]


def test_no_solution_when_substitute_still_violates(client):
    _cake_scenario(client)
    make_ingredient(
        client, "spelt_flour",
        allergens=[{"allergen": "gluten", "concentration": 8.0, "unit": "%"}],
    )
    resp = _remediate(
        client,
        substitutions=[{"ingredient": "wheat_flour", "substitute": "spelt_flour"}],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "no_solution"
    assert result["plan"] is None
    assert result["after"] is None
    assert [v["rule_code"] for v in result["before"]["violations"]] == ["gluten-prohibit"]


def test_no_solution_without_any_candidate(client):
    _cake_scenario(client)
    resp = _remediate(client, substitutions=[])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "no_solution"


def test_cheapest_plan_wins_by_substituted_mass(client):
    for code in ("wheat_a", "wheat_b"):
        make_ingredient(
            client, code,
            allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
        )
    make_ingredient(client, "corn_flour")
    make_ingredient(client, "sugar")
    _publish_set(
        client,
        "eu-pack",
        [
            {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 6000,
             "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
        ],
    )
    make_recipe(
        client,
        "mix",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat_a",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_b",
                     "quantity": 30, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 920, "unit": "g"},
                ],
            }
        ],
    )
    resp = _remediate(
        client,
        recipe_code="mix",
        substitutions=[
            {"ingredient": "wheat_a", "substitute": "corn_flour"},
            {"ingredient": "wheat_b", "substitute": "corn_flour"},
        ],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    # replacing either source alone clears the 6000 ppm threshold;
    # wheat_b is the lighter substitution (30 g < 50 g)
    assert result["plan"]["substitutions"] == [
        {"ingredient": "wheat_b", "substitute": "corn_flour",
         "substituted_mass_g": 30.0}
    ]
    assert result["plan"]["substituted_mass_g"] == 30.0


def test_equal_mass_plans_choose_lexicographically_smallest_pairs(client):
    for code in ("wheat_a", "wheat_b"):
        make_ingredient(
            client, code,
            allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
        )
    make_ingredient(client, "corn_flour")
    make_ingredient(client, "sugar")
    _publish_set(
        client,
        "eu-pack",
        [
            {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 6000,
             "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
        ],
    )
    make_recipe(
        client,
        "mix",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat_a",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_b",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 900, "unit": "g"},
                ],
            }
        ],
    )
    resp = _remediate(
        client,
        recipe_code="mix",
        substitutions=[
            {"ingredient": "wheat_b", "substitute": "corn_flour"},
            {"ingredient": "wheat_a", "substitute": "corn_flour"},
        ],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    # both single substitutions weigh 50 g; ("wheat_a", ...) sorts first
    assert [s["ingredient"] for s in result["plan"]["substitutions"]] == ["wheat_a"]


def test_plan_may_require_combining_substitutions(client):
    for code in ("wheat_a", "wheat_b"):
        make_ingredient(
            client, code,
            allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
        )
    make_ingredient(client, "corn_flour")
    make_ingredient(client, "sugar")
    _publish_set(
        client,
        "eu-pack",
        [
            {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 4000,
             "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
        ],
    )
    make_recipe(
        client,
        "mix",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat_a",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_b",
                     "quantity": 50, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar",
                     "quantity": 900, "unit": "g"},
                ],
            }
        ],
    )
    resp = _remediate(
        client,
        recipe_code="mix",
        substitutions=[
            {"ingredient": "wheat_a", "substitute": "corn_flour"},
            {"ingredient": "wheat_b", "substitute": "corn_flour"},
        ],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    # either single replacement still leaves 5000 ppm >= 4000 ppm
    assert [s["ingredient"] for s in result["plan"]["substitutions"]] == [
        "wheat_a",
        "wheat_b",
    ]
    assert result["plan"]["substituted_mass_g"] == 100.0
    assert result["after"]["violations"] == []


def test_cross_contact_prohibit_can_be_remediated(client):
    make_ingredient(
        client,
        "almond_flour",
        allergens=[{"allergen": "tree_nuts", "concentration": 100.0, "unit": "%"}],
        cross_contact=["peanut"],
    )
    make_ingredient(
        client,
        "hazelnut_flour",
        allergens=[{"allergen": "tree_nuts", "concentration": 100.0, "unit": "%"}],
    )
    _publish_set(
        client,
        "eu-pack",
        [
            {"code": "peanut-prohibit", "allergen": "peanut", "threshold": 0,
             "unit": "ppm", "action": "prohibit", "applies_to": "cross_contact"},
        ],
    )
    make_recipe(
        client,
        "paste",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "almond_flour",
                     "quantity": 100, "unit": "g"}
                ],
            }
        ],
    )
    resp = _remediate(
        client,
        recipe_code="paste",
        substitutions=[
            {"ingredient": "almond_flour", "substitute": "hazelnut_flour"}
        ],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    assert [v["rule_code"] for v in result["before"]["violations"]] == [
        "peanut-prohibit"
    ]
    assert result["after"]["violations"] == []
    # the substitute carries no cross-contact, so the peanut trace is gone
    assert result["after"]["warnings"] == []


def test_substitute_introduces_its_own_markers(client):
    _cake_scenario(client)
    make_ingredient(client, "oat_flour", cross_contact=["sesame"])
    assert client.put(
        "/rule-sets/eu-pack",
        json={
            "base_version": 2,
            "rules": RULES
            + [
                {"code": "sesame-warning", "allergen": "sesame", "threshold": 0,
                 "unit": "ppm", "action": "warning", "applies_to": "cross_contact"}
            ],
        },
    ).status_code == 200
    assert client.post(
        "/rule-sets/eu-pack/publish", json={"base_version": 3}
    ).status_code == 200

    resp = _remediate(
        client,
        rule_set_version=2,
        substitutions=[{"ingredient": "wheat_flour", "substitute": "oat_flour"}],
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "remediated"
    # the substitute's own cross-contact marker is recomputed into the plan
    sesame = [w for w in result["after"]["warnings"] if w["allergen"] == "sesame"]
    assert len(sesame) == 1
    paths = ["/".join(p["path"]) for p in sesame[0]["trigger_paths"]]
    assert paths == ["cake@v1/oat_flour"]
    assert sesame[0]["trigger_paths"][0]["ingredient"] == "oat_flour"


def test_remediation_replays_the_bound_snapshot(client):
    _cake_scenario(client)
    # v2 raises the prohibit threshold above the cake's gluten concentration
    assert client.put(
        "/rule-sets/eu-pack",
        json={
            "base_version": 2,
            "rules": [
                dict(r, threshold=100001) if r["code"] == "gluten-prohibit" else r
                for r in RULES
            ],
        },
    ).status_code == 200
    assert client.post(
        "/rule-sets/eu-pack/publish", json={"base_version": 3}
    ).status_code == 200

    substitution = [{"ingredient": "wheat_flour", "substitute": "rice_flour"}]
    v1 = _remediate(client, rule_set_version=1, substitutions=substitution).json()
    assert v1["status"] == "remediated"
    v2 = _remediate(client, rule_set_version=2, substitutions=substitution).json()
    # under the v2 snapshot the original recipe no longer violates anything
    assert v2["status"] == "compliant"
    assert v2["plan"] == {"substitutions": [], "substituted_mass_g": 0.0}


def test_duplicate_substitution_rejected(client):
    _cake_scenario(client)
    resp = _remediate(
        client,
        substitutions=[
            {"ingredient": "wheat_flour", "substitute": "rice_flour"},
            {"ingredient": "wheat_flour", "substitute": "sugar"},
        ],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "DUPLICATE_SUBSTITUTION"


def test_identical_substitution_rejected(client):
    _cake_scenario(client)
    resp = _remediate(
        client,
        substitutions=[{"ingredient": "wheat_flour", "substitute": "wheat_flour"}],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "IDENTICAL_SUBSTITUTION"


def test_substitution_of_absent_ingredient_rejected(client):
    _cake_scenario(client)
    make_ingredient(client, "barley_flour")
    resp = _remediate(
        client,
        substitutions=[{"ingredient": "barley_flour", "substitute": "rice_flour"}],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INGREDIENT_NOT_IN_RECIPE"


def test_unknown_substitute_rejected(client):
    _cake_scenario(client)
    resp = _remediate(
        client,
        substitutions=[{"ingredient": "wheat_flour", "substitute": "ghost_flour"}],
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UNKNOWN_SUBSTITUTE"


def test_recipe_reference_errors_match_evaluation(client):
    _cake_scenario(client)
    resp = _remediate(client, recipe_code="ghost")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"

    resp = _remediate(client, recipe_version=9)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"

    resp = client.post(
        "/recipes/cake/versions",
        json={
            "version": 2,
            "items": [
                {"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "kg"}
            ],
        },
    )
    assert resp.status_code == 201
    resp = _remediate(client, recipe_version=2)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "VERSION_NOT_PUBLISHED"


def test_rule_set_reference_errors_match_evaluation(client):
    _cake_scenario(client)
    resp = _remediate(client, rule_set="ghost-pack")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_NOT_FOUND"

    resp = _remediate(client, rule_set_version=9)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_VERSION_NOT_FOUND"

    resp = _remediate(client, jurisdiction="US")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RULE_SET_JURISDICTION_MISMATCH"


def test_incomplete_input_uses_existing_response(client):
    make_ingredient(client, "sugar")
    _publish_set(client, "eu-pack", RULES)
    make_recipe(
        client,
        "broken",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "mystery",
                     "quantity": 10, "unit": "g"}
                ],
            }
        ],
    )
    resp = _remediate(client, recipe_code="broken")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "INCOMPLETE_INPUT"
    assert detail["missing"] == [
        {"type": "ingredient", "code": "mystery", "reason": "unknown_ingredient"}
    ]


def test_dry_run_writes_nothing(client):
    published = _cake_scenario(client)
    resp = _remediate(
        client, substitutions=[{"ingredient": "wheat_flour", "substitute": "rice_flour"}]
    )
    assert resp.status_code == 200

    # the recipe version still references the original ingredient
    cake = client.get("/recipes/cake/versions/1").json()
    assert cake["items"] == [
        {"kind": "recipe", "code": "filling", "version": None,
         "quantity": 400.0, "unit": "g"},
        {"kind": "ingredient", "code": "wheat_flour", "version": None,
         "quantity": 1.0, "unit": "kg"},
    ]
    # the rule-set snapshot is untouched
    snapshot = client.get("/rule-sets/eu-pack/versions/1").json()
    assert snapshot["digest"] == published["digest"]
    # no evaluation history was created
    assert client.get("/evaluations").json() == []
