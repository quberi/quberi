#!/usr/bin/env bash
# Enrich the `acme-b` demo tenant with a realistic multi-department shape.
#
# What this adds on top of the bootstrap data:
#   * Org units:      Engineering { AI Platform, Frontend }, Product, Operations
#   * Positions:      AI 平台工程师 (P5), 前端负责人 (M2), 前端工程师 (P5),
#                     产品经理 (M1)
#   * Members:        Frank, Grace, Henry, Ivy, Jack (invited)
#   * Spaces:         Engineering Wiki, Frontend Sprint, Product Roadmap
#   * Bindings:       org-unit / position / user grants across the new spaces
#   * Group members:  the AI 作战小组 gets three new members (a position, an
#                     org unit, and a direct user) so every group hit-path
#                     is exercised.
#
# The script is idempotent-*ish*: it tolerates repeat runs by grep-guarding
# every creation and by ignoring 409/AlreadyExists on invite / grant calls.
# Run it after the initial acme-b bootstrap.

set -euo pipefail

TOKEN=${QUBERI_PLATFORM_ADMIN_TOKEN:-quberi-local-admin-2026}
API=${QUBERI_API:-http://localhost:8000}
ORG=${ORG_SLUG:-acme-b}
CALLER_EMAIL=${CALLER_EMAIL:-director@acme-b.example}

hdr=(
  -H "Authorization: Bearer $TOKEN"
  -H "X-Quberi-Organization: $ORG"
  -H "X-Quberi-User-Email: $CALLER_EMAIL"
  -H "Content-Type: application/json"
)

call() {  # method path [body]
  local method=$1 path=$2 body=${3:-}
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "${hdr[@]}" -d "$body" "$API$path"
  else
    curl -sS -X "$method" "${hdr[@]}" "$API$path"
  fi
}

log() { printf '\033[36m%s\033[0m\n' "$*" 1>&2; }
warn() { printf '\033[33m%s\033[0m\n' "$*" 1>&2; }
info() { printf '%s\n' "$*" 1>&2; }

# ---------- helpers ----------
find_unit_id() {  # $1 = name
  call GET /api/v1/organizations/current/org-units \
    | N="$1" python3 -c "import json,sys,os; name=os.environ['N']; d=json.load(sys.stdin); ids=[u['id'] for u in d['items'] if u['name']==name]; print(ids[0] if ids else '')"
}
find_position_id() {  # $1 = name
  call GET /api/v1/organizations/current/positions \
    | N="$1" python3 -c "import json,sys,os; name=os.environ['N']; d=json.load(sys.stdin); ids=[p['id'] for p in d['items'] if p['name']==name]; print(ids[0] if ids else '')"
}
find_user_id() {  # $1 = email
  call GET /api/v1/organizations/current/members \
    | E="$1" python3 -c "import json,sys,os; e=os.environ['E']; d=json.load(sys.stdin); ids=[m['id'] for m in d['items'] if m['email']==e]; print(ids[0] if ids else '')"
}
find_group_id() {  # $1 = name
  call GET /api/v1/organizations/current/groups \
    | N="$1" python3 -c "import json,sys,os; n=os.environ['N']; d=json.load(sys.stdin); ids=[g['id'] for g in d['items'] if g['name']==n]; print(ids[0] if ids else '')"
}
find_space_id() {  # $1 = slug
  call GET /api/v1/spaces \
    | S="$1" python3 -c "import json,sys,os; s=os.environ['S']; d=json.load(sys.stdin); ids=[x['id'] for x in d['items'] if x['slug']==s]; print(ids[0] if ids else '')"
}

# ---------- 1. Org units ----------
log '== 1. Org units =='

ensure_unit() {  # name type parent_id_or_empty
  local name=$1 type=$2 parent_id=${3:-}
  local id
  id=$(find_unit_id "$name")
  if [[ -n "$id" ]]; then
    info "  · $name already exists ($id)"
    echo "$id"; return
  fi
  local body
  if [[ -n "$parent_id" ]]; then
    body=$(printf '{"name":"%s","type":"%s","parent_id":"%s"}' "$name" "$type" "$parent_id")
  else
    body=$(printf '{"name":"%s","type":"%s"}' "$name" "$type")
  fi
  local resp
  resp=$(call POST /api/v1/organizations/current/org-units "$body")
  id=$(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))")
  if [[ -z "$id" ]]; then
    warn "    failed: $resp"
    return 1
  fi
  info "  + $name created ($id)"
  echo "$id"
}

ENG=$(ensure_unit "Engineering" department "" | tail -1)
PROD=$(ensure_unit "Product"     department "" | tail -1)
OPS=$(ensure_unit "Operations"   department "" | tail -1)
AIP=$(ensure_unit "AI Platform"  team       "$ENG" | tail -1)
FE=$(ensure_unit  "Frontend"     team       "$ENG" | tail -1)

# ---------- 2. Positions ----------
log '== 2. Positions =='

HEAD_POS=$(find_position_id "AI 平台负责人")
SR_POS=$(find_position_id   "AI 平台高级工程师")

ensure_position() {  # name grade org_unit_id reports_to_id headcount
  local name=$1 grade=$2 ou=$3 rt=$4 hc=$5
  local id
  id=$(find_position_id "$name")
  if [[ -n "$id" ]]; then
    info "  · $name already exists ($id)"
    echo "$id"; return
  fi
  local body
  body=$(python3 -c "
import json,sys
p = {'name': '$name', 'grade': '$grade', 'headcount_budget': $hc}
if '$ou': p['organization_unit_id'] = '$ou'
if '$rt': p['reports_to_position_id'] = '$rt'
print(json.dumps(p))
")
  local resp
  resp=$(call POST /api/v1/organizations/current/positions "$body")
  id=$(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))")
  if [[ -z "$id" ]]; then
    warn "    failed: $resp"
    return 1
  fi
  info "  + $name created ($id)"
  echo "$id"
}

JR_POS=$(ensure_position "AI 平台工程师" P5 "$AIP" "$HEAD_POS" 8    | tail -1)
FE_LEAD=$(ensure_position "前端负责人"   M2 "$FE"  ""          1    | tail -1)
FE_ENG=$(ensure_position  "前端工程师"   P5 "$FE"  "$FE_LEAD"  5    | tail -1)
PM_POS=$(ensure_position  "产品经理"     M1 "$PROD" ""         3    | tail -1)

# ---------- 3. Members ----------
log '== 3. Members =='

ensure_member() {  # email name role
  local email=$1 name=$2 role=$3
  local id
  id=$(find_user_id "$email")
  if [[ -n "$id" ]]; then
    info "  · $email already exists ($id)"
    echo "$id"; return
  fi
  local body
  body=$(printf '{"email":"%s","display_name":"%s","role":"%s"}' "$email" "$name" "$role")
  local resp
  resp=$(call POST /api/v1/organizations/current/members "$body")
  id=$(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id', ''))")
  if [[ -z "$id" ]]; then
    warn "    invite failed for $email: $resp"
    return 1
  fi
  info "  + $email invited ($id)"
  echo "$id"
}

FRANK=$(ensure_member frank@acme-b.example "Frank Frontend"    member | tail -1)
GRACE=$(ensure_member grace@acme-b.example "Grace Junior"      member | tail -1)
HENRY=$(ensure_member henry@acme-b.example "Henry Product"     member | tail -1)
IVY=$(ensure_member   ivy@acme-b.example   "Ivy Junior FE"     member | tail -1)
JACK=$(ensure_member  jack@acme-b.example  "Jack Ops"          member | tail -1)

# ---------- 4. Org-unit assignments ----------
log '== 4. Org-unit assignments =='

assign_unit() {  # unit_id user_id make_primary(true|false)
  local unit=$1 user=$2 primary=${3:-true}
  local body
  body=$(printf '{"user_id":"%s","make_primary":%s}' "$user" "$primary")
  local resp
  resp=$(call POST "/api/v1/organizations/current/org-units/$unit/members" "$body" || true)
  local ok
  ok=$(printf '%s' "$resp" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print('id' in d or 'membership_id' in d)
except Exception:
    print('False')")
  if [[ "$ok" == "True" ]]; then
    info "  + $user -> unit $unit (primary=$primary)"
  else
    info "  · $user -> unit $unit (already?) $resp"
  fi
}

# Ellie & Grace under AI Platform, Frank & Ivy under Frontend, Henry under
# Product, Jack under Operations. Diana / Peter stay at root (no unit).
ELLIE=$(find_user_id sr-eng@acme-b.example)
PETER=$(find_user_id pm@acme-b.example)

[[ -n "$AIP" && -n "$ELLIE" ]] && assign_unit "$AIP" "$ELLIE" true
[[ -n "$AIP" && -n "$GRACE" ]] && assign_unit "$AIP" "$GRACE" true
[[ -n "$FE"  && -n "$FRANK" ]] && assign_unit "$FE"  "$FRANK" true
[[ -n "$FE"  && -n "$IVY"   ]] && assign_unit "$FE"  "$IVY"   true
[[ -n "$PROD" && -n "$HENRY" ]] && assign_unit "$PROD" "$HENRY" true
[[ -n "$OPS"  && -n "$JACK"  ]] && assign_unit "$OPS"  "$JACK"  true

# ---------- 5. Position assignments ----------
log '== 5. Position assignments =='

assign_pos() {  # position_id user_id primary
  local pos=$1 user=$2 primary=${3:-true}
  local body
  body=$(printf '{"user_id":"%s","is_primary":%s}' "$user" "$primary")
  local resp
  resp=$(call POST "/api/v1/organizations/current/positions/$pos/occupants" "$body" || true)
  local ok
  ok=$(printf '%s' "$resp" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print('tenure_id' in d)
except Exception:
    print('False')")
  if [[ "$ok" == "True" ]]; then
    info "  + $user -> pos $pos (primary=$primary)"
  else
    info "  · $user -> pos $pos (already?) $resp"
  fi
}

[[ -n "$FE_LEAD" && -n "$FRANK" ]] && assign_pos "$FE_LEAD" "$FRANK" true
[[ -n "$JR_POS"  && -n "$GRACE" ]] && assign_pos "$JR_POS"  "$GRACE" true
[[ -n "$PM_POS"  && -n "$HENRY" ]] && assign_pos "$PM_POS"  "$HENRY" true
[[ -n "$FE_ENG"  && -n "$IVY"   ]] && assign_pos "$FE_ENG"  "$IVY"   true

# ---------- 6. Spaces ----------
log '== 6. Spaces =='

ensure_space() {  # name slug description
  local name=$1 slug=$2 desc=$3
  local id
  id=$(find_space_id "$slug")
  if [[ -n "$id" ]]; then
    info "  · $slug already exists ($id)"
    echo "$id"; return
  fi
  local body
  body=$(python3 -c "
import json,sys
print(json.dumps({
    'name': '$name',
    'slug': '$slug',
    'description': '$desc',
    'visibility': 'organization',
    'trust_level': 'trusted',
}))
")
  local resp
  resp=$(call POST /api/v1/spaces "$body")
  id=$(printf '%s' "$resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))")
  if [[ -z "$id" ]]; then
    warn "    space create failed: $resp"
    return 1
  fi
  info "  + $slug created ($id)"
  echo "$id"
}

ENG_WIKI=$(ensure_space "Engineering Wiki" engineering-wiki  "Shared engineering handbook (org-unit bound to Engineering)." | tail -1)
FE_SPACE=$(ensure_space "Frontend Sprint"  frontend-sprint   "Frontend team weekly cadence." | tail -1)
PM_SPACE=$(ensure_space "Product Roadmap"  product-roadmap   "Rolling product priorities." | tail -1)

# ---------- 7. Space access bindings ----------
log '== 7. Space bindings =='

grant() {  # space_id subject_type subject_id role
  local space=$1 stype=$2 sid=$3 role=$4
  local body
  body=$(printf '{"subject_type":"%s","subject_id":"%s","role":"%s"}' "$stype" "$sid" "$role")
  local resp
  resp=$(call POST "/api/v1/spaces/$space/access-bindings" "$body" || true)
  local ok
  ok=$(printf '%s' "$resp" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print('id' in d)
except Exception:
    print('False')")
  if [[ "$ok" == "True" ]]; then
    info "  + $space $stype($sid) -> $role"
  else
    info "  · $space $stype($sid) -> $role (already?) $resp"
  fi
}

# Engineering Wiki: whole Engineering subtree gets viewer.
[[ -n "$ENG_WIKI" && -n "$ENG" ]] && grant "$ENG_WIKI" organization_unit "$ENG" viewer

# Frontend Sprint: FE lead is editor, whole Frontend team is viewer.
[[ -n "$FE_SPACE" && -n "$FE_LEAD" ]] && grant "$FE_SPACE" position "$FE_LEAD" editor
[[ -n "$FE_SPACE" && -n "$FE"      ]] && grant "$FE_SPACE" organization_unit "$FE" viewer

# Product Roadmap: PMs edit; Peter (AI lead) reviews (cross-function).
[[ -n "$PM_SPACE" && -n "$PM_POS" ]] && grant "$PM_SPACE" position "$PM_POS" editor
[[ -n "$PM_SPACE" && -n "$PETER"  ]] && grant "$PM_SPACE" user     "$PETER"  viewer

# ---------- 8. Group enrichment ----------
log '== 8. Group members =='

STRIKE=$(find_group_id "AI 作战小组")

add_group_member() {  # group_id member_type member_id
  local gid=$1 mtype=$2 mid=$3
  local body
  body=$(printf '{"member_type":"%s","member_id":"%s"}' "$mtype" "$mid")
  local resp
  resp=$(call POST "/api/v1/organizations/current/groups/$gid/members" "$body" || true)
  local ok
  ok=$(printf '%s' "$resp" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin)
    print('id' in d)
except Exception:
    print('False')")
  if [[ "$ok" == "True" ]]; then
    info "  + group $gid += $mtype($mid)"
  else
    info "  · group $gid += $mtype($mid) (already?) $resp"
  fi
}

if [[ -n "$STRIKE" ]]; then
  # Ellie's position joins so occupants inherit; AI Platform org-unit joins
  # so anyone in the team inherits; Frank joins as a plain user member.
  [[ -n "$SR_POS" ]] && add_group_member "$STRIKE" position          "$SR_POS"
  [[ -n "$AIP"    ]] && add_group_member "$STRIKE" organization_unit "$AIP"
  [[ -n "$FRANK"  ]] && add_group_member "$STRIKE" user              "$FRANK"
fi

log ''
log '== Done. Effective-access on Ops Playbook: =='
OP=$(find_space_id ops-playbook)
if [[ -n "$OP" ]]; then
  call GET "/api/v1/spaces/$OP/effective-members" \
    | python3 -c "
import json,sys
d = json.load(sys.stdin)
for m in d['items']:
    paths = ', '.join(p['hit_path'] for p in m['hit_paths'])
    print(f\"  {m['display_name'].ljust(20)} {m['effective_role'].ljust(8)}  via  {paths}\")
"
fi
