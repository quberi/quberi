"""Enrich the ``acme-b`` demo tenant to a stress-test scale.

Idempotent-ish: every create is first probed with a list-and-match; only
missing entities are created. Failures print a short reason and keep going.

Coverage after this runs:
  * ≥ 10 org units (adds Backend / Mobile / Data / QA under Engineering,
    Product Design under Product, HR / Finance / Legal under Operations,
    Marketing as a root unit).
  * ≥ 1 org unit with ≥ 20 direct members (Backend).
  * ≥ 1 position with ≥ 20 current occupants (后端工程师, headcount 25).
  * ≥ 10 groups (adds eleven of them).
  * ≥ 1 group with ≥ 20 direct GroupMember rows (稳定性专项).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("QUBERI_PLATFORM_ADMIN_TOKEN", "quberi-local-admin-2026")
API = os.environ.get("QUBERI_API", "http://localhost:8000")
ORG = os.environ.get("ORG_SLUG", "acme-b")
CALLER = os.environ.get("CALLER_EMAIL", "director@acme-b.example")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _request(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Quberi-Organization": ORG,
        "X-Quberi-User-Email": CALLER,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        raise ApiError(e.code, str(detail))


def get(path):
    return _request("GET", path)


def post(path, body):
    return _request("POST", path, body)


# ---------------------------------------------------------------------------
# Ensure helpers
# ---------------------------------------------------------------------------


def ensure_unit(name: str, unit_type: str, parent_id: str | None) -> str:
    existing = {u["name"]: u["id"] for u in get("/api/v1/organizations/current/org-units")["items"]}
    if name in existing:
        print(f"  · unit {name} already exists ({existing[name]})")
        return existing[name]
    body = {"name": name, "type": unit_type}
    if parent_id:
        body["parent_id"] = parent_id
    resp = post("/api/v1/organizations/current/org-units", body)
    print(f"  + unit {name} created ({resp['id']})")
    return resp["id"]


def ensure_position(
    name: str,
    grade: str,
    org_unit_id: str | None,
    reports_to_id: str | None,
    headcount: int,
) -> str:
    existing = {
        p["name"]: p["id"]
        for p in get("/api/v1/organizations/current/positions")["items"]
    }
    if name in existing:
        print(f"  · position {name} already exists ({existing[name]})")
        return existing[name]
    body = {"name": name, "grade": grade, "headcount_budget": headcount}
    if org_unit_id:
        body["organization_unit_id"] = org_unit_id
    if reports_to_id:
        body["reports_to_position_id"] = reports_to_id
    resp = post("/api/v1/organizations/current/positions", body)
    print(f"  + position {name} created ({resp['id']}, hc={headcount})")
    return resp["id"]


def ensure_member(email: str, name: str, role: str = "member") -> str:
    existing = {
        m["email"]: m["id"]
        for m in get("/api/v1/organizations/current/members")["items"]
    }
    if email in existing:
        return existing[email]
    try:
        resp = post(
            "/api/v1/organizations/current/members",
            {"email": email, "display_name": name, "role": role},
        )
        return resp["id"]
    except ApiError as e:
        if e.status == 409:
            # Race: someone else invited concurrently, re-fetch.
            return {m["email"]: m["id"] for m in get(
                "/api/v1/organizations/current/members"
            )["items"]}[email]
        raise


def assign_unit(unit_id: str, user_id: str, primary: bool = True) -> None:
    try:
        post(
            f"/api/v1/organizations/current/org-units/{unit_id}/members",
            {"user_id": user_id, "make_primary": primary},
        )
    except ApiError as e:
        if e.status != 409:
            print(f"    ! assign_unit {user_id}->{unit_id}: {e.status} {e.detail}")


def assign_position(position_id: str, user_id: str, primary: bool = True) -> None:
    try:
        post(
            f"/api/v1/organizations/current/positions/{position_id}/occupants",
            {"user_id": user_id, "is_primary": primary},
        )
    except ApiError as e:
        if e.status != 409:
            print(f"    ! assign_position {user_id}->{position_id}: {e.status} {e.detail}")


def ensure_group(name: str, group_type: str = "project", description: str = "") -> str:
    existing = {
        g["name"]: g["id"]
        for g in get("/api/v1/organizations/current/groups")["items"]
    }
    if name in existing:
        print(f"  · group {name} already exists ({existing[name]})")
        return existing[name]
    resp = post(
        "/api/v1/organizations/current/groups",
        {"name": name, "type": group_type, "description": description},
    )
    print(f"  + group {name} created ({resp['id']})")
    return resp["id"]


def add_group_member(group_id: str, member_type: str, member_id: str) -> None:
    try:
        post(
            f"/api/v1/organizations/current/groups/{group_id}/members",
            {"member_type": member_type, "member_id": member_id},
        )
    except ApiError as e:
        if e.status != 409:
            print(f"    ! add_group_member: {e.status} {e.detail}")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"[*] target={API}  org={ORG}  caller={CALLER}")
    t0 = time.time()

    # ==================================================================
    # 1. Org units
    # ==================================================================
    print("== 1. Org units ==")
    unit_ids: dict[str, str] = {}

    # Existing roots we expect from earlier seeding.
    for name in ("Engineering", "Product", "Operations", "AI Platform", "Frontend"):
        rows = [
            u for u in get("/api/v1/organizations/current/org-units")["items"]
            if u["name"] == name
        ]
        if rows:
            unit_ids[name] = rows[0]["id"]

    # Root additions
    unit_ids["Marketing"] = ensure_unit("Marketing", "department", None)

    # Under Engineering
    for name in ("Backend", "Mobile", "Data", "QA"):
        unit_ids[name] = ensure_unit(name, "team", unit_ids["Engineering"])

    # Under Product
    unit_ids["Product Design"] = ensure_unit(
        "Product Design", "team", unit_ids["Product"]
    )

    # Under Operations
    for name in ("HR", "Finance", "Legal"):
        unit_ids[name] = ensure_unit(name, "team", unit_ids["Operations"])

    # ==================================================================
    # 2. Positions
    # ==================================================================
    print("== 2. Positions ==")
    pos_ids: dict[str, str] = {}
    for p in get("/api/v1/organizations/current/positions")["items"]:
        pos_ids[p["name"]] = p["id"]

    # Layer 1: department leads (M2). Standalone reporting.
    pos_ids["后端负责人"] = ensure_position(
        "后端负责人", "M2", unit_ids["Backend"], None, 1
    )
    pos_ids["移动端负责人"] = ensure_position(
        "移动端负责人", "M2", unit_ids["Mobile"], None, 1
    )
    pos_ids["数据负责人"] = ensure_position(
        "数据负责人", "M2", unit_ids["Data"], None, 1
    )
    pos_ids["QA 负责人"] = ensure_position(
        "QA 负责人", "M2", unit_ids["QA"], None, 1
    )

    # Layer 2: engineers reporting to their unit's lead.
    pos_ids["后端工程师"] = ensure_position(
        "后端工程师", "P5", unit_ids["Backend"], pos_ids["后端负责人"], 25
    )
    pos_ids["移动端工程师"] = ensure_position(
        "移动端工程师", "P5", unit_ids["Mobile"], pos_ids["移动端负责人"], 10
    )
    pos_ids["数据工程师"] = ensure_position(
        "数据工程师", "P5", unit_ids["Data"], pos_ids["数据负责人"], 10
    )
    pos_ids["QA 工程师"] = ensure_position(
        "QA 工程师", "P5", unit_ids["QA"], pos_ids["QA 负责人"], 10
    )

    # Layer 3: support / operations roles
    pos_ids["产品设计师"] = ensure_position(
        "产品设计师", "P5", unit_ids["Product Design"], None, 5
    )
    pos_ids["HR 专员"] = ensure_position("HR 专员", "P5", unit_ids["HR"], None, 3)
    pos_ids["财务专员"] = ensure_position("财务专员", "P5", unit_ids["Finance"], None, 3)
    pos_ids["法务专员"] = ensure_position("法务专员", "P5", unit_ids["Legal"], None, 3)
    pos_ids["市场经理"] = ensure_position("市场经理", "M1", unit_ids["Marketing"], None, 5)

    # ==================================================================
    # 3. Users
    # ==================================================================
    print("== 3. Users ==")

    def hires(prefix: str, n: int, first_names: list[str]) -> list[tuple[str, str]]:
        """Generate ``n`` (email, display_name) pairs. Wraps names if n exceeds
        the pool, appending a numeric suffix to preserve uniqueness."""

        out = []
        for i in range(n):
            first = first_names[i % len(first_names)]
            suffix = "" if i < len(first_names) else str(i // len(first_names) + 1)
            email = f"{prefix}{i + 1:02d}@acme-b.example"
            name = f"{first}{suffix} {prefix.upper()}"
            out.append((email, name))
        return out

    backend_hires = hires(
        "be",
        20,
        ["Alan", "Bella", "Chris", "Dora", "Eddy", "Fiona", "Gary", "Hana",
         "Ian", "Jenna", "Kyle", "Luna", "Mark", "Nina", "Oscar", "Priya",
         "Quinn", "Riko", "Sam", "Tara"],
    )
    mobile_hires = hires("mo", 3, ["Yusuf", "Zoe", "Aria"])
    data_hires = hires("dt", 4, ["Bo", "Cai", "Dawn", "Elin"])
    qa_hires = hires("qa", 5, ["Fumi", "Gia", "Haruto", "Ines", "Jonas"])
    design_hires = hires("pd", 3, ["Kito", "Lena", "Mimi"])
    hr_hires = hires("hr", 2, ["Nikhil", "Ola"])
    fin_hires = hires("fn", 2, ["Pia", "Quentin"])
    legal_hires = hires("lg", 2, ["Ravi", "Sina"])
    mkt_hires = hires("mk", 3, ["Tomo", "Uma", "Vivi"])

    lead_hires = [
        ("be-lead@acme-b.example", "Beatrice Lead"),
        ("mo-lead@acme-b.example", "Miguel Lead"),
        ("dt-lead@acme-b.example", "Deepa Lead"),
        ("qa-lead@acme-b.example", "Quan Lead"),
    ]

    all_hires = (
        backend_hires
        + mobile_hires
        + data_hires
        + qa_hires
        + design_hires
        + hr_hires
        + fin_hires
        + legal_hires
        + mkt_hires
        + lead_hires
    )
    user_ids: dict[str, str] = {}
    for i, (email, name) in enumerate(all_hires, 1):
        uid = ensure_member(email, name)
        user_ids[email] = uid
        if i % 10 == 0:
            print(f"  · invited {i}/{len(all_hires)}")

    print(f"  = invited {len(all_hires)} users total")

    # ==================================================================
    # 4. Org-unit + position assignments
    # ==================================================================
    print("== 4. Assignments ==")

    def wire(unit_name: str, position_name: str | None, hire_list) -> None:
        unit_id = unit_ids[unit_name]
        pos_id = pos_ids.get(position_name) if position_name else None
        for email, _ in hire_list:
            uid = user_ids[email]
            assign_unit(unit_id, uid, primary=True)
            if pos_id:
                assign_position(pos_id, uid, primary=True)

    wire("Backend", "后端工程师", backend_hires)
    wire("Mobile", "移动端工程师", mobile_hires)
    wire("Data", "数据工程师", data_hires)
    wire("QA", "QA 工程师", qa_hires)
    wire("Product Design", "产品设计师", design_hires)
    wire("HR", "HR 专员", hr_hires)
    wire("Finance", "财务专员", fin_hires)
    wire("Legal", "法务专员", legal_hires)
    wire("Marketing", "市场经理", mkt_hires)

    # Leads: one per unit, on the M2 position.
    lead_assignments = [
        ("Backend", "后端负责人", "be-lead@acme-b.example"),
        ("Mobile", "移动端负责人", "mo-lead@acme-b.example"),
        ("Data", "数据负责人", "dt-lead@acme-b.example"),
        ("QA", "QA 负责人", "qa-lead@acme-b.example"),
    ]
    for unit_name, pos_name, email in lead_assignments:
        uid = user_ids[email]
        assign_unit(unit_ids[unit_name], uid, primary=True)
        assign_position(pos_ids[pos_name], uid, primary=True)

    # ==================================================================
    # 5. Groups
    # ==================================================================
    print("== 5. Groups ==")

    group_ids: dict[str, str] = {}
    group_ids["稳定性专项"] = ensure_group(
        "稳定性专项", "project", "Cross-team reliability task force (large)."
    )
    group_ids["前端周会"] = ensure_group("前端周会", "project", "Frontend team weekly.")
    group_ids["后端周会"] = ensure_group("后端周会", "project", "Backend team weekly.")
    group_ids["安全审计"] = ensure_group(
        "安全审计", "security", "Security review board."
    )
    group_ids["Hackathon 2026"] = ensure_group(
        "Hackathon 2026", "project", "Company hackathon 2026 cohort."
    )
    group_ids["Onboarding Cohort 2026"] = ensure_group(
        "Onboarding Cohort 2026", "project", "New-hire onboarding cohort."
    )
    group_ids["数据治理"] = ensure_group("数据治理", "project", "Data governance council.")
    group_ids["QA 巡逻组"] = ensure_group("QA 巡逻组", "project", "QA rotation squad.")
    group_ids["招聘评审组"] = ensure_group(
        "招聘评审组", "security", "Hiring review committee."
    )
    group_ids["财务审阅组"] = ensure_group(
        "财务审阅组", "security", "Finance approvals committee."
    )
    group_ids["全员大会"] = ensure_group("全员大会", "static", "Company-wide broadcast list.")

    # ==================================================================
    # 6. Group members
    # ==================================================================
    print("== 6. Group members ==")

    def uid(email: str) -> str:
        return user_ids[email]

    # ---- 稳定性专项: 20+ direct user members (the stress case) ----
    reliability_users = (
        backend_hires[:15]
        + mobile_hires
        + data_hires[:2]
        + qa_hires[:3]
        + [("be-lead@acme-b.example", ""), ("mo-lead@acme-b.example", ""), ("dt-lead@acme-b.example", "")]
    )
    print(f"  稳定性专项: adding {len(reliability_users)} direct user members")
    for email, _ in reliability_users:
        add_group_member(group_ids["稳定性专项"], "user", uid(email))
    # Sprinkle two non-user members too, to exercise the polymorphic renderer.
    add_group_member(group_ids["稳定性专项"], "position", pos_ids["后端负责人"])
    add_group_member(group_ids["稳定性专项"], "organization_unit", unit_ids["QA"])

    # ---- 前端周会 / 后端周会: position + org_unit hits ----
    add_group_member(group_ids["前端周会"], "position", pos_ids["前端负责人"])
    add_group_member(group_ids["前端周会"], "organization_unit", unit_ids["Frontend"])

    add_group_member(group_ids["后端周会"], "position", pos_ids["后端负责人"])
    add_group_member(group_ids["后端周会"], "organization_unit", unit_ids["Backend"])

    # ---- 安全审计: all *_lead users + Legal unit ----
    for email, _ in lead_hires:
        add_group_member(group_ids["安全审计"], "user", uid(email))
    add_group_member(group_ids["安全审计"], "organization_unit", unit_ids["Legal"])

    # ---- Hackathon 2026: 10 mixed engineers as user members ----
    hackathon_pool = (
        backend_hires[15:20]
        + design_hires
        + mobile_hires
        + data_hires[:1]
        + qa_hires[:1]
    )
    for email, _ in hackathon_pool[:10]:
        add_group_member(group_ids["Hackathon 2026"], "user", uid(email))

    # ---- Onboarding: 6 fresh hires ----
    onboarding_pool = (
        backend_hires[-3:] + design_hires[-1:] + hr_hires + qa_hires[:1]
    )
    for email, _ in onboarding_pool:
        add_group_member(group_ids["Onboarding Cohort 2026"], "user", uid(email))

    # ---- 数据治理: Data unit + position + Finance lead as user ----
    add_group_member(group_ids["数据治理"], "organization_unit", unit_ids["Data"])
    add_group_member(group_ids["数据治理"], "position", pos_ids["数据工程师"])

    # ---- QA 巡逻组: QA unit + one FE + one BE lead ----
    add_group_member(group_ids["QA 巡逻组"], "organization_unit", unit_ids["QA"])
    add_group_member(group_ids["QA 巡逻组"], "user", uid("be-lead@acme-b.example"))
    add_group_member(group_ids["QA 巡逻组"], "user", uid("qa-lead@acme-b.example"))

    # ---- 招聘评审组: HR unit + every lead as user ----
    add_group_member(group_ids["招聘评审组"], "organization_unit", unit_ids["HR"])
    for email, _ in lead_hires:
        add_group_member(group_ids["招聘评审组"], "user", uid(email))

    # ---- 财务审阅组: Finance unit + Diana + Legal lead ----
    add_group_member(group_ids["财务审阅组"], "organization_unit", unit_ids["Finance"])
    add_group_member(group_ids["财务审阅组"], "organization_unit", unit_ids["Legal"])

    # ---- 全员大会: all three top-level departments as org_units ----
    add_group_member(group_ids["全员大会"], "organization_unit", unit_ids["Engineering"])
    add_group_member(group_ids["全员大会"], "organization_unit", unit_ids["Product"])
    add_group_member(group_ids["全员大会"], "organization_unit", unit_ids["Operations"])
    add_group_member(group_ids["全员大会"], "organization_unit", unit_ids["Marketing"])

    dt = time.time() - t0
    print()
    print(f"[*] done in {dt:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApiError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
