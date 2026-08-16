"""
Fictional seed data. Every name, balance, and ID here is invented for this
assignment -- no real PII, no real institution.
"""

MEMBERS: dict[str, dict] = {
    "4521": {
        "member_id": "4521",
        "name": "Dana Whitfield",
        "savings_balance": "2,410.55",
        "status": "Active",
    },
    "8832": {
        "member_id": "8832",
        "name": "Marcus Ojo",
        "savings_balance": "918.20",
        "status": "Active",
    },
    "1001": {
        "member_id": "1001",
        "name": "Priya Nandakumar",
        "savings_balance": "5,002.00",
        "status": "Active",
    },
    "1002": {
        "member_id": "1002",
        "name": "Priya Nandakumar",  # same person, Tenant B record
        "savings_balance": "5,002.00",
        "status": "Active",
    },
    # 6600 is deliberately "restricted" to exercise the permission-denied path
    "6600": {
        "member_id": "6600",
        "name": "Restricted Record",
        "savings_balance": "REDACTED",
        "status": "Restricted",
    },
}

# any member_id not in MEMBERS and not in RESTRICTED below => "not found"
RESTRICTED_IDS = {"6600"}

SUPERVISOR_CODE = "2468"  # fictional, used for the "risky action" confirmation demo
