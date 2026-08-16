"""
Tenant configuration.

Two tenants run the *same* underlying vendor product (this Flask app) but are
configured/branded/versioned differently -- exactly the "hundreds of tenants,
same vendor product" reality described in the assignment brief. This is what
lets us later demonstrate an artifact recorded on Tenant A being replayed
against Tenant B via a small override patch, rather than re-recorded from
scratch.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    display_name: str
    route_prefix: str          # e.g. "/desk" vs "/operations"
    member_id_label: str       # field label differs across tenants
    member_id_field_name: str  # HTML form field name differs across tenants
    # order in which columns render in the member detail table -- a classic
    # "same data, different layout" drift source between tenant instances
    detail_columns: tuple[str, ...]
    theme_class: str


TENANTS: dict[str, TenantConfig] = {
    "a": TenantConfig(
        tenant_id="a",
        display_name="CorePoint Teller Desk",
        route_prefix="/desk",
        member_id_label="Member ID",
        member_id_field_name="member_id",
        detail_columns=("name", "member_id", "savings_balance", "status"),
        theme_class="theme-a",
    ),
    "b": TenantConfig(
        tenant_id="b",
        display_name="Northwind Credit Union Operations",
        route_prefix="/operations",
        member_id_label="Acct Holder No.",
        member_id_field_name="acct_holder_no",
        detail_columns=("savings_balance", "name", "status", "member_id"),
        theme_class="theme-b",
    ),
}


def get_tenant(tenant_id: str) -> TenantConfig:
    if tenant_id not in TENANTS:
        raise KeyError(f"Unknown tenant: {tenant_id}")
    return TENANTS[tenant_id]
