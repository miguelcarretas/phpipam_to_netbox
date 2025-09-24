"""Command line interface for exporting phpIPAM data to NetBox."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .api import (
    PhpIPAMAuthenticationError,
    PhpIPAMClient,
    PhpIPAMError,
)
from .converters import (
    AddressRecord,
    CustomerRecord,
    ExportSettings,
    SubnetRecord,
    build_customer_records,
    build_ip_rows,
    build_prefix_rows,
    build_tenant_rows,
    convert_addresses,
    convert_subnets,
    determine_used_customer_ids,
    slugify,
    write_csv,
)

LOG_FORMAT = "%(levelname)s: %(message)s"
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export subnets and IP addresses from phpIPAM and produce NetBox import files.",
    )
    parser.add_argument("--phpipam-url", required=True, help="Base URL of the phpIPAM installation")
    parser.add_argument("--app-id", required=True, help="phpIPAM API application identifier")
    parser.add_argument("--token", help="Pre-generated phpIPAM API token")
    parser.add_argument("--username", help="Username for phpIPAM API authentication")
    parser.add_argument("--password", help="Password for phpIPAM API authentication")
    parser.add_argument(
        "--verify-ssl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Verify TLS certificates when talking to phpIPAM (default: enabled)",
    )
    parser.add_argument(
        "--customer",
        action="append",
        default=[],
        help="Restrict the export to the specified phpIPAM customer (name or ID). Repeat to include multiple customers.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory where the NetBox import files will be written",
    )
    parser.add_argument(
        "--prefix-status",
        default="active",
        help="NetBox status used for exported prefixes (default: active)",
    )
    parser.add_argument(
        "--ip-status",
        default="active",
        help="Fallback NetBox status used for exported IP addresses (default: active)",
    )
    parser.add_argument(
        "--map-tags-to-status",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Translate phpIPAM address tags into NetBox statuses (default: enabled)",
    )
    parser.add_argument(
        "--tag-status-map",
        help=(
            "Custom mapping from phpIPAM tag identifiers or names to NetBox statuses. "
            "Example: '1=active,Reserved=reserved'"
        ),
    )
    parser.add_argument(
        "--tenant-group",
        help="Optional NetBox tenant group assigned to all generated tenants",
    )
    parser.add_argument(
        "--map-customers-to-tenants",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Create tenants in NetBox for phpIPAM customers (default: enabled)",
    )
    parser.add_argument(
        "--skip-ip-addresses",
        action="store_true",
        help="Do not export individual IP addresses",
    )
    parser.add_argument(
        "--skip-prefixes",
        action="store_true",
        help="Do not export prefixes",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Verbosity of log output",
    )
    return parser


def parse_tag_status_map(raw_value: Optional[str]) -> Dict[str, str]:
    if not raw_value:
        return {}

    result: Dict[str, str] = {}
    for part in raw_value.split(","):
        if "=" not in part:
            raise ValueError(f"Invalid tag mapping '{part}'. Use the form <tag>=<status>.")
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def resolve_customer_filters(
    customers: Sequence[CustomerRecord], queries: Sequence[str]
) -> Optional[List[str]]:
    if not queries:
        return None

    by_id = {customer.id: customer for customer in customers}
    by_name = {customer.name.lower(): customer for customer in customers}
    by_slug = {customer.slug: customer for customer in customers}

    resolved: List[str] = []
    missing: List[str] = []
    for query in queries:
        cleaned = query.strip()
        if not cleaned:
            continue
        candidate = None
        if cleaned in by_id:
            candidate = by_id[cleaned]
        elif cleaned.lower() in by_name:
            candidate = by_name[cleaned.lower()]
        elif slugify(cleaned) in by_slug:
            candidate = by_slug[slugify(cleaned)]
        if candidate:
            resolved.append(candidate.id)
        else:
            missing.append(cleaned)

    if missing:
        raise SystemExit(f"Unknown customer(s): {', '.join(missing)}")

    return resolved


def load_tag_mapping(overrides: Mapping[str, str], tag_payload: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    if not overrides:
        return {}

    by_id = {str(tag.get("id")): str(tag.get("id")) for tag in tag_payload}
    by_name: Dict[str, str] = {}
    for tag in tag_payload:
        name_value = tag.get("type") or tag.get("name")
        if isinstance(name_value, str) and name_value.strip():
            by_name[name_value.strip().lower()] = str(tag.get("id"))

    result: Dict[str, str] = {}
    for key, value in overrides.items():
        if key in by_id:
            result[key] = value
            continue
        lower_key = key.lower()
        if lower_key in by_name:
            result[by_name[lower_key]] = value
            continue
        result[key] = value
    return result


def collect_addresses(
    client: PhpIPAMClient,
    subnets: Sequence[SubnetRecord],
) -> Dict[str, List[Mapping[str, Any]]]:
    all_addresses: Dict[str, List[Mapping[str, Any]]] = {}
    for subnet in subnets:
        try:
            addresses = client.get_addresses_for_subnet(subnet.id)
        except PhpIPAMError as exc:
            logger.warning("Failed to fetch addresses for subnet %s: %s", subnet.id, exc)
            continue
        all_addresses[subnet.id] = addresses
    return all_addresses


def export_data(args: argparse.Namespace) -> None:
    logging.basicConfig(level=getattr(logging, args.log_level), format=LOG_FORMAT)

    client = PhpIPAMClient(
        base_url=args.phpipam_url,
        app_id=args.app_id,
        username=args.username,
        password=args.password,
        token=args.token,
        verify_ssl=args.verify_ssl,
    )

    try:
        customers_raw = client.get_customers()
    except PhpIPAMAuthenticationError as exc:
        raise SystemExit(f"Authentication with phpIPAM failed: {exc}") from exc
    except PhpIPAMError as exc:
        logger.error("Failed to retrieve customer information: %s", exc)
        customers_raw = []

    customer_records, customer_index = build_customer_records(customers_raw)
    customer_filter = resolve_customer_filters(customer_records, args.customer)

    try:
        subnets_raw = client.get_all_subnets()
    except PhpIPAMError as exc:
        raise SystemExit(f"Failed to retrieve subnets from phpIPAM: {exc}") from exc

    # Filter out folder entries and optionally by customer
    filtered_subnets: List[Mapping[str, Any]] = []
    allowed_customers = set(customer_filter) if customer_filter else None
    for subnet in subnets_raw:
        if str(subnet.get("isFolder", "0")) == "1":
            logger.debug("Skipping folder entry %s", subnet.get("description"))
            continue
        if allowed_customers is not None:
            customer_id = subnet.get("customer_id") or subnet.get("customerId")
            if str(customer_id) not in allowed_customers:
                continue
        filtered_subnets.append(subnet)

    if not filtered_subnets:
        logger.warning("No subnets matched the provided criteria.")

    settings = ExportSettings(
        prefix_status=args.prefix_status,
        ip_status=args.ip_status,
        map_customer_to_tenant=args.map_customers_to_tenants,
        tenant_group=args.tenant_group,
        map_tags_to_status=args.map_tags_to_status,
    )

    tag_payload: List[Mapping[str, Any]] = []
    if settings.map_tags_to_status or args.tag_status_map:
        try:
            tag_payload = client.get_ip_tags()
        except PhpIPAMError as exc:
            logger.info("Unable to retrieve phpIPAM address tags: %s", exc)
            tag_payload = []

    try:
        tag_overrides = parse_tag_status_map(args.tag_status_map)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    custom_map = load_tag_mapping(tag_overrides, tag_payload)
    if custom_map:
        settings.custom_tag_status_map = custom_map

    subnet_records = convert_subnets(filtered_subnets, customer_index=customer_index, settings=settings)
    subnet_index = {record.id: record for record in subnet_records}

    address_records: List[AddressRecord] = []
    if not args.skip_ip_addresses:
        addresses_raw = collect_addresses(client, subnet_records)
        address_records = convert_addresses(
            addresses_raw,
            subnets=subnet_index,
            customer_index=customer_index,
            settings=settings,
        )

    used_customer_ids = determine_used_customer_ids(subnet_records, address_records)
    used_customers = [customer_index[cid] for cid in used_customer_ids if cid in customer_index]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if settings.map_customer_to_tenant and used_customers:
        tenant_rows = build_tenant_rows(used_customers)
        tenant_fields = ["name", "slug", "description"]
        write_csv(output_dir / "tenants.csv", tenant_rows, tenant_fields)

    if not args.skip_prefixes:
        prefix_rows = build_prefix_rows(subnet_records, customers=customer_index, settings=settings)
        prefix_fields = ["prefix", "status", "description", "tenant", "tenant__slug", "tenant_group"]
        write_csv(output_dir / "prefixes.csv", prefix_rows, prefix_fields)

    if not args.skip_ip_addresses and address_records:
        ip_rows = build_ip_rows(address_records, customers=customer_index, settings=settings)
        ip_fields = ["address", "status", "dns_name", "description", "tenant", "tenant__slug", "tenant_group"]
        write_csv(output_dir / "ip-addresses.csv", ip_rows, ip_fields)

    logger.info(
        "Export completed: %d prefixes and %d IP addresses processed.",
        len(subnet_records),
        len(address_records),
    )


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    export_data(args)


if __name__ == "__main__":  # pragma: no cover
    main()
