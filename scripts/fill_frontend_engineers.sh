#!/usr/bin/env bash
# One-off: fill 前端工程师 up to its headcount by inviting four new hires
# and assigning them to Frontend + the position.
set -euo pipefail

TOKEN=${QUBERI_PLATFORM_ADMIN_TOKEN:-quberi-local-admin-2026}
API=${QUBERI_API:-http://localhost:8000}
ORG=acme-b
CALLER=director@acme-b.example

hdr=(
  -H "Authorization: Bearer $TOKEN"
  -H "X-Quberi-Organization: $ORG"
  -H "X-Quberi-User-Email: $CALLER"
  -H "Content-Type: application/json"
)

call() {
  local method=$1 path=$2 body=${3:-}
  if [[ -n "$body" ]]; then
    curl -sS -m 10 -X "$method" "${hdr[@]}" -d "$body" "$API$path"
  else
    curl -sS -m 10 -X "$method" "${hdr[@]}" "$API$path"
  fi
}

FE_UNIT=$(call GET /api/v1/organizations/current/org-units \
  | python3 -c "import json,sys; [print(u['id']) for u in json.load(sys.stdin)['items'] if u['name']=='Frontend']")
FE_POS=$(call GET /api/v1/organizations/current/positions \
  | python3 -c "import json,sys; [print(p['id']) for p in json.load(sys.stdin)['items'] if p['name']=='前端工程师']")

echo "Frontend unit: $FE_UNIT"
echo "前端工程师 pos: $FE_POS"
echo

for entry in \
  "kevin@acme-b.example|Kevin Kim" \
  "lily@acme-b.example|Lily Liu" \
  "max@acme-b.example|Max Mendes" \
  "nora@acme-b.example|Nora Ng"; do

  EMAIL=${entry%%|*}
  NAME=${entry##*|}
  echo "--- $NAME ($EMAIL) ---"

  # Invite
  resp=$(call POST /api/v1/organizations/current/members \
    "$(printf '{"email":"%s","display_name":"%s","role":"member"}' "$EMAIL" "$NAME")")
  USER_ID=$(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))")
  if [[ -z "$USER_ID" ]]; then
    echo "  invite failed: $resp"
    continue
  fi
  echo "  invited: $USER_ID"

  # Assign to Frontend org-unit (primary)
  resp=$(call POST "/api/v1/organizations/current/org-units/$FE_UNIT/members" \
    "$(printf '{"user_id":"%s","make_primary":true}' "$USER_ID")")
  echo "  org-unit: $(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if 'membership_id' in d else 'err: '+str(d.get('detail',d)))")"

  # Assign 前端工程师 as primary position
  resp=$(call POST "/api/v1/organizations/current/positions/$FE_POS/occupants" \
    "$(printf '{"user_id":"%s","is_primary":true}' "$USER_ID")")
  echo "  position: $(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok tenure='+d['tenure_id'] if 'tenure_id' in d else 'err: '+str(d.get('detail',d)))")"
done

echo
echo "=== After ==="
call GET /api/v1/organizations/current/positions | python3 -c "
import json, sys
for p in json.load(sys.stdin)['items']:
    if p['name'] == '前端工程师':
        print(f\"  {p['name']}: {p['current_occupants']}/{p['headcount_budget']}\")"
