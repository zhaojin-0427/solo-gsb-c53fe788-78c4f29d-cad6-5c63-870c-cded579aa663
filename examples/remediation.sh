#!/usr/bin/env bash
# End-to-end remediation-preview example against a locally running API
# (default: http://localhost:8000).
#
#   bash examples/remediation.sh
#
# The preview is read-only: it never creates a recipe version, rule set or
# evaluation row, so it can be replayed freely.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
j() { python3 -m json.tool; }

echo "== 1. health =="
curl -fsS "$BASE/health"; echo

echo "== 2. register ingredients =="
# wheat: 10% gluten (the violating leaf); oat: 50 ppm gluten (still over the
# 20 ppm prohibit when used 1:1); rice: gluten-free (the remediation target).
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "wheat_flour",
  "name": "Wheat Flour",
  "allergens": [{"allergen": "gluten", "concentration": 10, "unit": "%"}]
}' | j
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "oat_flour",
  "name": "Oat Flour",
  "allergens": [{"allergen": "gluten", "concentration": 50, "unit": "ppm"}]
}' | j
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"code": "rice_flour", "name": "Rice Flour"}' | j

echo "== 3. publish a 1 kg wheat loaf =="
curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"loaf","name":"Plain Loaf"}' | j
curl -fsS -X POST "$BASE/recipes/loaf/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [{"kind": "ingredient", "code": "wheat_flour", "quantity": 1, "unit": "kg"}]
}' | j
curl -fsS -X POST "$BASE/recipes/loaf/versions/1/publish" | j

echo "== 4. publish the EU rule set (declare at 0 ppm, prohibit at 20 ppm) =="
curl -fsS -X POST "$BASE/rule-sets" -H 'Content-Type: application/json' \
  -d '{"code":"eu-pack","jurisdiction":"EU","name":"EU allergen pack"}' | j
curl -fsS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' -d '{
  "base_version": 0,
  "rules": [
    {"code":"gluten-declare","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"gluten-prohibit","allergen":"gluten","threshold":20,"unit":"ppm","action":"prohibit","applies_to":"contains"}
  ]
}' >/dev/null
curl -fsS -X POST "$BASE/rule-sets/eu-pack/publish" \
  -H 'Content-Type: application/json' -d '{"base_version":1}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("published", d["rule_set_code"], "v"+str(d["version"]), d["digest"])'

echo "== 5. remediation preview: wheat -> rice removes the prohibit (200) =="
curl -fsS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf",
  "recipe_version": 1,
  "jurisdiction": "EU",
  "rule_set_code": "eu-pack",
  "rule_set_version": 1,
  "substitutions": [
    {"ingredient": "wheat_flour", "alternative": "rice_flour"}
  ]
}' | j

echo "== 6. no solution: the only alternative still breaches 20 ppm (200, no_solution) =="
curl -fsS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf",
  "recipe_version": 1,
  "jurisdiction": "EU",
  "rule_set_code": "eu-pack",
  "rule_set_version": 1,
  "substitutions": [
    {"ingredient": "wheat_flour", "alternative": "oat_flour"}
  ]
}' | j

echo "== 7. invalid mappings -> 422 =="
echo "-- duplicate source ingredient --"
curl -sS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
  "rule_set_code": "eu-pack", "rule_set_version": 1,
  "substitutions": [
    {"ingredient": "wheat_flour", "alternative": "rice_flour"},
    {"ingredient": "wheat_flour", "alternative": "oat_flour"}
  ]
}' | j || true
echo "-- unknown alternative ingredient --"
curl -sS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
  "rule_set_code": "eu-pack", "rule_set_version": 1,
  "substitutions": [{"ingredient": "wheat_flour", "alternative": "no_such_flour"}]
}' | j || true
echo "-- source ingredient not present in the recipe --"
curl -sS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
  "rule_set_code": "eu-pack", "rule_set_version": 1,
  "substitutions": [{"ingredient": "rice_flour", "alternative": "oat_flour"}]
}' | j || true
echo "-- same ingredient as its own alternative --"
curl -sS -X POST "$BASE/remediations" -H 'Content-Type: application/json' -d '{
  "recipe_code": "loaf", "recipe_version": 1, "jurisdiction": "EU",
  "rule_set_code": "eu-pack", "rule_set_version": 1,
  "substitutions": [{"ingredient": "wheat_flour", "alternative": "wheat_flour"}]
}' | j || true

echo "== 8. the preview wrote nothing: recipe still published, no evaluations =="
curl -fsS "$BASE/recipes/loaf/versions/1" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("recipe v1:", d["status"], "first item:", d["items"][0]["code"])'
curl -fsS "$BASE/evaluations" | python3 -c 'import sys,json; print("stored evaluations:", len(json.load(sys.stdin)))'
