"""Allergen remediation preview (``POST /remediations``).

The preview reuses the real expansion + scoring engines, rewrites leaf
ingredient codes in memory, and returns the unique minimum-mass plan that
removes every prohibit violation. These tests cover selection, tie-breaks,
validation, stable reference errors and the no-write guarantee.
"""

from tests.helpers import make_ingredient, make_recipe


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


GLUTEN_PACK = [
    {"code": "gluten-declare", "allergen": "gluten", "threshold": 0,
     "unit": "ppm", "action": "declare", "applies_to": "contains"},
    {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 20,
     "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
]


def _remediate(client, substitutions, recipe="loaf", version=1,
               jurisdiction="EU", set_code="eu-pack", set_version=1):
    return client.post(
        "/remediations",
        json={
            "recipe_code": recipe,
            "recipe_version": version,
            "jurisdiction": jurisdiction,
            "rule_set_code": set_code,
            "rule_set_version": set_version,
            "substitutions": substitutions,
        },
    )


def _loaf(client):
    # 1 kg wheat flour at 10% gluten -> 100,000 ppm in a 1000 g batch.
    make_ingredient(
        client,
        "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(client, "rice")
    make_recipe(
        client,
        "loaf",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}
                ],
            }
        ],
    )


def _assert_scores_are_complete(payload):
    for side in ("before", "after"):
        section = payload[side]
        if section is None:
            continue
        for key in (
            "status", "total_mass_g", "allergens", "obligations",
            "warnings", "violations", "unregulated_allergens",
        ):
            assert key in section
        assert section["recipe"] == payload["recipe"]
        assert section["jurisdiction"] == payload["jurisdiction"]
        assert section["rule_set"] == payload["rule_set"]


# --------------------------------------------------------------------------
# Happy paths: remediated / already compliant / no solution
# --------------------------------------------------------------------------

def test_substitution_removes_prohibit_and_all_obligations(client):
    _loaf(client)
    pub = _publish_set(client, "eu-pack", GLUTEN_PACK)

    resp = _remediate(client, [{"ingredient": "wheat", "alternative": "rice"}])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["status"] == "remediated"
    assert data["plan"] == [
        {"ingredient": "wheat", "alternative": "rice", "replaced_mass_g": 1000.0}
    ]
    assert data["replaced_total_mass_g"] == 1000.0
    assert data["recipe"] == {"code": "loaf", "version": 1}
    assert data["jurisdiction"] == "EU"
    assert data["rule_set"] == {
        "code": "eu-pack", "version": 1, "digest": pub["digest"],
    }
    assert {"code": "loaf", "version": 1} in data["resolved_recipes"]

    before, after = data["before"], data["after"]
    assert before["total_mass_g"] == after["total_mass_g"] == 1000.0
    assert [v["rule_code"] for v in before["violations"]] == ["gluten-prohibit"]
    assert [o["rule_code"] for o in before["obligations"]] == ["gluten-declare"]
    assert after["violations"] == []
    assert after["obligations"] == []
    assert after["warnings"] == []
    assert after["allergens"] == []
    _assert_scores_are_complete(data)


def test_declare_survives_when_allergen_remains_but_prohibit_is_cleared(client):
    # wheat carries the whole gluten load; a mildly glutenous oat keeps the
    # declare (threshold 0) but drops below the 20 ppm prohibit.
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(
        client, "oat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "ppm"}],
    )
    make_recipe(
        client, "loaf",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}]}])
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    data = _remediate(client, [{"ingredient": "wheat", "alternative": "oat"}]).json()
    assert data["status"] == "remediated"
    assert data["after"]["violations"] == []
    assert [o["rule_code"] for o in data["after"]["obligations"]] == ["gluten-declare"]
    assert data["after"]["allergens"] == [
        {"allergen": "gluten", "concentration_ppm": 10.0, "concentration_percent": 0.001}
    ]


def test_already_compliant_returns_empty_plan_with_substitution(client):
    _loaf(client)
    _publish_set(
        client, "eu-pack",
        [
            {"code": "gluten-declare", "allergen": "gluten", "threshold": 0,
             "unit": "ppm", "action": "declare", "applies_to": "contains"},
            {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 1000000,
             "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
        ],
    )
    resp = _remediate(client, [{"ingredient": "wheat", "alternative": "rice"}])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "already_compliant"
    assert data["plan"] == []
    assert data["replaced_total_mass_g"] == 0.0
    # No substitution is applied: after equals the original evaluation.
    assert data["after"]["allergens"] == data["before"]["allergens"]
    assert data["after"]["obligations"] == data["before"]["obligations"]
    assert data["after"]["violations"] == []


def test_already_compliant_with_empty_substitution_list(client):
    _loaf(client)
    _publish_set(
        client, "eu-pack",
        [{"code": "p", "allergen": "gluten", "threshold": 1000000,
          "unit": "ppm", "action": "prohibit", "applies_to": "contains"}],
    )
    data = _remediate(client, []).json()
    assert data["status"] == "already_compliant"
    assert data["plan"] == []
    assert data["after"]["violations"] == []


def test_no_solution_returns_200_with_null_plan_and_after(client):
    # The only proposed alternative still breaches the 20 ppm threshold.
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(
        client, "rye",
        allergens=[{"allergen": "gluten", "concentration": 50, "unit": "ppm"}],
    )
    make_recipe(
        client, "loaf",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}]}])
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    resp = _remediate(client, [{"ingredient": "wheat", "alternative": "rye"}])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_solution"
    assert data["plan"] is None
    assert data["after"] is None
    assert data["replaced_total_mass_g"] == 0.0
    assert [v["rule_code"] for v in data["before"]["violations"]] == ["gluten-prohibit"]


def test_no_solution_with_empty_substitution_list(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    data = _remediate(client, []).json()
    assert data["status"] == "no_solution"
    assert data["plan"] is None and data["after"] is None


# --------------------------------------------------------------------------
# Selection: minimum mass, then lexicographic sorted-pair order
# --------------------------------------------------------------------------

def test_plan_minimizes_replaced_mass(client):
    # 950 g bulk (1% gluten -> 9.5 g) + 50 g dust (100% gluten -> 50 g),
    # 1000 g batch, prohibit at 20000 ppm (limit 20 g gluten). Replacing the
    # 50 g dust alone leaves 9500 ppm and is the minimum; replacing bulk alone
    # leaves 50000 ppm and is infeasible.
    make_ingredient(
        client, "bulk",
        allergens=[{"allergen": "gluten", "concentration": 1, "unit": "%"}],
    )
    make_ingredient(
        client, "dust",
        allergens=[{"allergen": "gluten", "concentration": 100, "unit": "%"}],
    )
    make_ingredient(client, "safe_bulk")
    make_ingredient(client, "safe_dust")
    make_recipe(
        client, "mix",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "bulk", "quantity": 950, "unit": "g"},
            {"kind": "ingredient", "code": "dust", "quantity": 50, "unit": "g"}]}])
    _publish_set(
        client, "eu-pack",
        [{"code": "p", "allergen": "gluten", "threshold": 20000,
          "unit": "ppm", "action": "prohibit", "applies_to": "contains"}],
    )
    data = _remediate(
        client,
        [{"ingredient": "bulk", "alternative": "safe_bulk"},
         {"ingredient": "dust", "alternative": "safe_dust"}],
        recipe="mix",
    ).json()
    assert data["status"] == "remediated"
    assert data["plan"] == [
        {"ingredient": "dust", "alternative": "safe_dust", "replaced_mass_g": 50.0}
    ]
    assert data["replaced_total_mass_g"] == 50.0


def test_equal_mass_tie_broken_by_pair_lexicographic_order(client):
    # 500 g m + 500 z, each 0.003 % gluten (15 ppm each, 30 ppm total),
    # prohibit 20 ppm. Either single swap clears it at identical 500 g mass;
    # the sorted pair starting with "m" wins.
    make_ingredient(
        client, "m",
        allergens=[{"allergen": "gluten", "concentration": 0.003, "unit": "%"}],
    )
    make_ingredient(
        client, "z",
        allergens=[{"allergen": "gluten", "concentration": 0.003, "unit": "%"}],
    )
    make_ingredient(client, "safe_m")
    make_ingredient(client, "safe_z")
    make_recipe(
        client, "mix",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "m", "quantity": 500, "unit": "g"},
            {"kind": "ingredient", "code": "z", "quantity": 500, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "p", "allergen": "gluten", "threshold": 20,
         "unit": "ppm", "action": "prohibit", "applies_to": "contains"}])

    # Input order deliberately reversed; the result must be identical.
    bodies = [
        [{"ingredient": "z", "alternative": "safe_z"},
         {"ingredient": "m", "alternative": "safe_m"}],
        [{"ingredient": "m", "alternative": "safe_m"},
         {"ingredient": "z", "alternative": "safe_z"}],
    ]
    results = [
        _remediate(client, subs, recipe="mix").json() for subs in bodies
    ]
    for data in results:
        assert data["status"] == "remediated"
        assert [pair["ingredient"] for pair in data["plan"]] == ["m"]
        assert data["replaced_total_mass_g"] == 500.0
        assert data["after"]["allergens"] == [
            {"allergen": "gluten", "concentration_ppm": 15.0,
             "concentration_percent": 0.0015}
        ]
    assert results[0] == results[1]


def test_lex_tie_break_is_tuple_not_subset_order(client):
    # Two feasible plans have EQUAL replaced mass but different cardinality:
    #   plan {"b"}       replaces 50 g,          pair tuple ("b",)
    #   plan {"a", "c"}  replaces 25 g + 25 g,   pair tuple ("a", "c")
    # Sorted-pair lexicographic order ranks ("a", "c") < ("b",) even though
    # {"a","c"} is the "larger" subset, so the implementation must pick it.
    #
    # 1000 g batch, prohibit 60 ppm (limit 0.06 g gluten), 900 g safe filler:
    #   a 25 g @ 0.08 %  -> 0.020 g gluten     c 25 g @ 0.12 % -> 0.030 g
    #   b 50 g @ 0.09 %  -> 0.045 g gluten
    make_ingredient(client, "a", allergens=[{"allergen": "gluten",
                                            "concentration": 0.08, "unit": "%"}])
    make_ingredient(client, "c", allergens=[{"allergen": "gluten",
                                            "concentration": 0.12, "unit": "%"}])
    make_ingredient(client, "b", allergens=[{"allergen": "gluten",
                                            "concentration": 0.09, "unit": "%"}])
    make_ingredient(client, "filler")
    for code in ("a", "b", "c"):
        make_ingredient(client, f"safe_{code}")
    make_recipe(
        client, "mix",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "a", "quantity": 25, "unit": "g"},
            {"kind": "ingredient", "code": "c", "quantity": 25, "unit": "g"},
            {"kind": "ingredient", "code": "b", "quantity": 50, "unit": "g"},
            {"kind": "ingredient", "code": "filler", "quantity": 900, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "p", "allergen": "gluten", "threshold": 60,
         "unit": "ppm", "action": "prohibit", "applies_to": "contains"}])
    # Sanity: baseline 95 ppm violates; {a} (75 ppm) and {c} (65 ppm) do not
    # clear it; {b} -> 50 ppm and {a,c} -> 45 ppm both clear it at 50 g.
    data = _remediate(
        client,
        [{"ingredient": "b", "alternative": "safe_b"},
         {"ingredient": "c", "alternative": "safe_c"},
         {"ingredient": "a", "alternative": "safe_a"}],
        recipe="mix",
    ).json()
    assert data["status"] == "remediated"
    assert [pair["ingredient"] for pair in data["plan"]] == ["a", "c"]
    assert data["replaced_total_mass_g"] == 50.0
    assert data["after"]["allergens"] == [
        {"allergen": "gluten", "concentration_ppm": 45.0,
         "concentration_percent": 0.0045}
    ]


def test_threshold_boundary_still_violates(client):
    # Equal-mass swap that lands exactly at 20 ppm keeps the prohibit (>=), so
    # that single mapping yields no_solution.
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(
        client, "oat20",
        allergens=[{"allergen": "gluten", "concentration": 20, "unit": "ppm"}],
    )
    make_ingredient(
        client, "rice19",
        allergens=[{"allergen": "gluten", "concentration": 19, "unit": "ppm"}],
    )
    make_recipe(
        client, "loaf",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "p", "allergen": "gluten", "threshold": 20,
         "unit": "ppm", "action": "prohibit", "applies_to": "contains"}])

    exact = _remediate(
        client, [{"ingredient": "wheat", "alternative": "oat20"}]).json()
    assert exact["status"] == "no_solution"

    below = _remediate(
        client, [{"ingredient": "wheat", "alternative": "rice19"}]).json()
    assert below["status"] == "remediated"
    assert below["after"]["violations"] == []


# --------------------------------------------------------------------------
# Nested expansion and cross-contact
# --------------------------------------------------------------------------

def test_nested_occurrences_all_replaced_equal_mass(client):
    # filling v1 = 250 g almond + 250 g sugar; cake = 500 g filling + 500 g
    # almond => 750 g almond in total (250 via sub-recipe + 500 direct).
    make_ingredient(
        client, "almond",
        allergens=[{"allergen": "tree_nuts", "concentration": 100, "unit": "%"}],
        cross_contact=["peanut"],
    )
    make_ingredient(client, "sugar")
    make_ingredient(client, "oat_flour")
    make_recipe(
        client, "filling",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "almond", "quantity": 250, "unit": "g"},
            {"kind": "ingredient", "code": "sugar", "quantity": 250, "unit": "g"}]}])
    make_recipe(
        client, "cake",
        [{"version": 1, "items": [
            {"kind": "recipe", "code": "filling", "quantity": 500, "unit": "g"},
            {"kind": "ingredient", "code": "almond", "quantity": 500, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "tn-p", "allergen": "tree_nuts", "threshold": 10,
         "unit": "ppm", "action": "prohibit", "applies_to": "contains"}])

    data = _remediate(
        client, [{"ingredient": "almond", "alternative": "oat_flour"}],
        recipe="cake",
    ).json()
    assert data["status"] == "remediated"
    assert data["plan"] == [
        {"ingredient": "almond", "alternative": "oat_flour",
         "replaced_mass_g": 750.0}
    ]
    assert data["replaced_total_mass_g"] == 750.0
    assert data["after"]["total_mass_g"] == 1000.0
    assert data["after"]["violations"] == []
    assert data["after"]["allergens"] == []
    assert {"code": "cake", "version": 1} in data["resolved_recipes"]
    assert {"code": "filling", "version": 1} in data["resolved_recipes"]


def test_cross_contact_prohibit_cleared_by_substitution(client):
    make_ingredient(client, "risky", cross_contact=["peanut"])
    make_ingredient(client, "clean", cross_contact=["sesame"])
    make_recipe(
        client, "bar",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "risky", "quantity": 100, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "pp", "allergen": "peanut", "threshold": 0, "unit": "ppm",
         "action": "prohibit", "applies_to": "cross_contact"}])
    data = _remediate(
        client, [{"ingredient": "risky", "alternative": "clean"}],
        recipe="bar",
    ).json()
    assert data["status"] == "remediated"
    assert data["before"]["violations"][0]["source"] == "cross_contact"
    assert data["after"]["violations"] == []


def test_cross_contact_prohibit_no_solution_when_alternative_also_marked(client):
    make_ingredient(client, "risky", cross_contact=["peanut"])
    make_ingredient(client, "also_risky", cross_contact=["peanut", "sesame"])
    make_recipe(
        client, "bar",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "risky", "quantity": 100, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "pp", "allergen": "peanut", "threshold": 0, "unit": "ppm",
         "action": "prohibit", "applies_to": "cross_contact"}])
    data = _remediate(
        client, [{"ingredient": "risky", "alternative": "also_risky"}],
        recipe="bar",
    ).json()
    assert data["status"] == "no_solution"


# --------------------------------------------------------------------------
# Validation errors (all 422)
# --------------------------------------------------------------------------

def test_duplicate_source_mapping_rejected(client):
    _loaf(client)
    make_ingredient(client, "rye")
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [
        {"ingredient": "wheat", "alternative": "rice"},
        {"ingredient": "wheat", "alternative": "rye"},
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "DUPLICATE_SUBSTITUTION"
    assert detail["ingredients"] == ["wheat"]


def test_identical_pair_listed_twice_rejected(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [
        {"ingredient": "wheat", "alternative": "rice"},
        {"ingredient": "wheat", "alternative": "rice"},
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "DUPLICATE_SUBSTITUTION"


def test_substitution_to_same_ingredient_rejected(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [{"ingredient": "wheat", "alternative": "wheat"}])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "SAME_SUBSTITUTION"
    assert detail["ingredients"] == ["wheat"]


def test_absent_source_ingredient_rejected(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [{"ingredient": "ghost", "alternative": "rice"}])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "SUBSTITUTION_SOURCE_NOT_USED"
    assert detail["ingredients"] == ["ghost"]


def test_unknown_alternative_ingredient_rejected(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [{"ingredient": "wheat", "alternative": "no_such"}])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "UNKNOWN_ALTERNATIVE_INGREDIENT"
    assert detail["ingredients"] == ["no_such"]


def test_multiple_invalid_codes_reported_stably_sorted(client):
    _loaf(client)
    make_ingredient(client, "rye")
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(client, [
        {"ingredient": "wheat", "alternative": "zz_missing"},
        {"ingredient": "wheat", "alternative": "rice"},
        {"ingredient": "rye", "alternative": "aa_missing"},
    ])
    assert resp.status_code == 422
    # Duplicate source takes precedence and lists the source once.
    assert resp.json()["detail"]["ingredients"] == ["wheat"]

    resp = _remediate(client, [
        {"ingredient": "wheat", "alternative": "zz_missing"},
        {"ingredient": "rye", "alternative": "aa_missing"},
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["ingredients"] == ["aa_missing", "zz_missing"]

    resp = _remediate(client, [
        {"ingredient": "rye", "alternative": "rice"},
        {"ingredient": "absent_b", "alternative": "wheat"},
        {"ingredient": "absent_a", "alternative": "rice"},
    ])
    assert resp.status_code == 422
    # rye is registered but not used in the loaf; absent_a/absent_b are simply
    # not present — all three are stably sorted source-not-used codes.
    assert resp.json()["detail"]["ingredients"] == ["absent_a", "absent_b", "rye"]


# --------------------------------------------------------------------------
# Reference errors reuse the evaluation endpoint's stable responses
# --------------------------------------------------------------------------

def test_rule_set_reference_errors(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    resp = client.post("/remediations", json={
        "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
        "rule_set_code": "ghost", "rule_set_version": 1, "substitutions": []})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_NOT_FOUND"

    resp = client.post("/remediations", json={
        "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
        "rule_set_code": "eu-pack", "rule_set_version": 99, "substitutions": []})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_VERSION_NOT_FOUND"


def test_jurisdiction_mismatch_rejected(client):
    _loaf(client)
    _publish_set(client, "us-pack", GLUTEN_PACK, jurisdiction="US")
    resp = _remediate(client, [], set_code="us-pack")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "RULE_SET_JURISDICTION_MISMATCH"
    assert detail["rule_set_jurisdiction"] == "US"
    assert detail["requested_jurisdiction"] == "EU"


def test_recipe_reference_errors(client):
    make_ingredient(client, "rice")
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    resp = _remediate(client, [], recipe="nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"

    make_recipe(
        client, "drafty",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "rice", "quantity": 1, "unit": "kg"}]}],
        publish=False)
    resp = _remediate(client, [], recipe="drafty")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "VERSION_NOT_PUBLISHED"


def test_incomplete_recipe_input_surfaces_minimal_missing_set(client):
    make_ingredient(client, "rice")
    make_recipe(
        client, "broken",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "mystery", "quantity": 10, "unit": "g"}]}])
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    resp = _remediate(
        client, [{"ingredient": "mystery", "alternative": "rice"}], recipe="broken"
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "INCOMPLETE_INPUT"
    assert detail["missing"] == [
        {"type": "ingredient", "code": "mystery", "reason": "unknown_ingredient"}
    ]


def test_bound_snapshot_is_immutable_for_remediation(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)  # v1: prohibit 20 ppm
    # Publish v2 that would make the loaf legal; the preview stays on v1.
    assert client.put("/rule-sets/eu-pack", json={
        "base_version": 2,
        "rules": [
            {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 1000000,
             "unit": "ppm", "action": "prohibit", "applies_to": "contains"}]
    }).status_code == 200
    assert client.post(
        "/rule-sets/eu-pack/publish", json={"base_version": 3}
    ).status_code == 200

    v1 = _remediate(client, [], set_version=1)
    assert v1.json()["status"] == "no_solution"
    v2 = _remediate(client, [], set_version=2)
    assert v2.json()["status"] == "already_compliant"


# --------------------------------------------------------------------------
# No-write guarantee and unchanged evaluation behavior
# --------------------------------------------------------------------------

def test_preview_does_not_modify_recipe_rules_or_evaluation_history(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    before_version = client.get("/recipes/loaf/versions/1").json()
    assert _remediate(
        client, [{"ingredient": "wheat", "alternative": "rice"}]
    ).status_code == 200

    after_version = client.get("/recipes/loaf/versions/1").json()
    assert after_version == before_version
    assert after_version["status"] == "published"
    assert after_version["items"][0]["code"] == "wheat"
    # No evaluation rows are produced by the preview.
    assert client.get("/evaluations").json() == []


def test_evaluation_and_idempotent_replay_unchanged(client):
    _loaf(client)
    _publish_set(client, "eu-pack", GLUTEN_PACK)

    first = client.post("/evaluations", json={
        "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
        "rule_set_code": "eu-pack", "rule_set_version": 1,
        "idempotency_key": "remediation-regression-1"})
    assert first.status_code == 201
    evaluation_id = first.json()["evaluation_id"]
    assert len(first.json()["violations"]) == 1

    replay = client.post("/evaluations", json={
        "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
        "rule_set_code": "eu-pack", "rule_set_version": 1,
        "idempotency_key": "remediation-regression-1"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["evaluation_id"] == evaluation_id

    conflict = client.post("/evaluations", json={
        "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "US",
        "rule_set_code": "eu-pack", "rule_set_version": 1,
        "idempotency_key": "remediation-regression-1"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_mappings_apply_to_original_leaf_codes_without_chaining(client):
    # Mappings wheat->oat and oat->rice are offered together. Selection is per
    # ORIGINAL leaf code: whichever subset wins, an original wheat leaf resolves
    # at most to oat (it is not chained wheat->oat->rice) and an original oat
    # leaf resolves at most to rice.
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(
        client, "oat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "ppm"}],
    )
    make_ingredient(client, "rice")
    make_recipe(
        client, "mix",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "wheat", "quantity": 500, "unit": "g"},
            {"kind": "ingredient", "code": "oat", "quantity": 500, "unit": "g"}]}])
    _publish_set(client, "eu-pack", [
        {"code": "g-declare", "allergen": "gluten", "threshold": 0,
         "unit": "ppm", "action": "declare", "applies_to": "contains"},
        {"code": "p", "allergen": "gluten", "threshold": 20,
         "unit": "ppm", "action": "prohibit", "applies_to": "contains"}])
    # Baseline ~50,005 ppm. wheat->oat makes the whole batch oat-equivalent at
    # 10 ppm (declare still fires, prohibit cleared; feasible at 500 g replaced);
    # oat->rice leaves 50,000 ppm (infeasible); both -> zero at 1000 g (heavier).
    # The minimum plan is wheat->oat alone; the engine must look each ORIGINAL
    # leaf code up exactly once, so wheat leaves that became oat are not chained
    # on to rice.
    data = _remediate(
        client,
        [{"ingredient": "wheat", "alternative": "oat"},
         {"ingredient": "oat", "alternative": "rice"}],
        recipe="mix",
    ).json()
    assert data["status"] == "remediated"
    assert [pair["ingredient"] for pair in data["plan"]] == ["wheat"]
    assert data["replaced_total_mass_g"] == 500.0
    assert data["after"]["allergens"] == [
        {"allergen": "gluten", "concentration_ppm": 10.0,
         "concentration_percent": 0.001}
    ]
    contributing = {
        p["ingredient"]
        for section in (data["after"]["obligations"], data["after"]["violations"])
        for entry in section for p in entry["trigger_paths"]
    }
    assert contributing == {"oat"}


def test_remediation_is_deterministic_across_repeated_calls(client):
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_ingredient(client, "rice")
    make_recipe(
        client, "loaf",
        [{"version": 1, "items": [
            {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}]}])
    _publish_set(client, "eu-pack", GLUTEN_PACK)
    payload = {"recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
               "rule_set_code": "eu-pack", "rule_set_version": 1,
               "substitutions": [{"ingredient": "wheat", "alternative": "rice"}]}
    r1 = client.post("/remediations", json=payload)
    r2 = client.post("/remediations", json=payload)
    assert r1.json() == r2.json()
    # Still nothing persisted.
    assert client.get("/evaluations").json() == []
