"""Translate phpIPAM objects into NetBox import structures."""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from ipaddress import ip_address, ip_interface, ip_network
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Default mapping between phpIPAM address tags and NetBox status values. The
# built-in tag identifiers are stable but users can customise them. The CLI
# allows overriding these mappings when necessary.
DEFAULT_TAG_STATUS_MAP: Mapping[str, str] = {
    "1": "active",      # Used
    "2": "reserved",    # Reserved
    "3": "dhcp",        # DHCP
    "4": "deprecated",  # Offline
}


@dataclass
class ExportSettings:
    """Configuration values that influence the export process."""

    prefix_status: str = "active"
    ip_status: str = "active"
    map_customer_to_tenant: bool = True
    tenant_group: Optional[str] = None
    map_tags_to_status: bool = True
    custom_tag_status_map: Optional[Mapping[str, str]] = None

    def address_status_for_tag(self, tag_id: Optional[str]) -> str:
        """Return the NetBox status for a phpIPAM address tag."""

        if not self.map_tags_to_status or not tag_id:
            return self.ip_status

        tag_map = dict(DEFAULT_TAG_STATUS_MAP)
        if self.custom_tag_status_map:
            tag_map.update({str(key): value for key, value in self.custom_tag_status_map.items()})

        return tag_map.get(str(tag_id), self.ip_status)


@dataclass
class CustomerRecord:
    """Representation of a phpIPAM customer."""

    id: str
    name: str
    slug: str
    description: Optional[str] = None


@dataclass
class SubnetRecord:
    """Representation of a phpIPAM subnet in a format suitable for NetBox."""

    id: str
    prefix: str
    description: Optional[str]
    customer_id: Optional[str]
    is_pool: bool
    is_full: bool
    section_id: Optional[str]


@dataclass
class AddressRecord:
    """Representation of a phpIPAM IP address."""

    id: str
    address: str
    description: Optional[str]
    hostname: Optional[str]
    customer_id: Optional[str]
    tag: Optional[str]


def slugify(value: str) -> str:
    """Convert ``value`` into a slug that works for NetBox."""

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-") or "tenant"


def determine_customer_name(customer: Mapping[str, Any]) -> str:
    """Return the best effort display name for a phpIPAM customer."""

    for key in ("name", "title", "customer", "description"):
        value = customer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Customer {customer.get('id')}"


def determine_customer_description(customer: Mapping[str, Any]) -> Optional[str]:
    for key in ("description", "note", "notes"):
        value = customer.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def decode_ip_value(value: Any) -> str:
    """Return a textual representation for phpIPAM IP address values."""

    if value is None:
        raise ValueError("Cannot decode an empty IP address value")

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return str(ip_address(int(value)))

    value = str(value).strip()
    return str(ip_address(value))


def build_customer_records(customers: Iterable[Mapping[str, Any]]) -> Tuple[List[CustomerRecord], Dict[str, CustomerRecord]]:
    """Create :class:`CustomerRecord` objects from raw phpIPAM responses."""

    records: List[CustomerRecord] = []
    seen_slugs: MutableMapping[str, int] = {}
    by_id: Dict[str, CustomerRecord] = {}

    for customer in customers:
        raw_id = customer.get("id")
        if raw_id is None:
            continue
        identifier = str(raw_id)
        name = determine_customer_name(customer)
        description = determine_customer_description(customer)

        base_slug = slugify(name)
        slug = base_slug
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}-{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 1

        record = CustomerRecord(id=identifier, name=name, slug=slug, description=description)
        records.append(record)
        by_id[identifier] = record

    return records, by_id


def convert_subnets(subnets: Sequence[Mapping[str, Any]], *, customer_index: Mapping[str, CustomerRecord], settings: ExportSettings) -> List[SubnetRecord]:
    """Convert raw subnet dictionaries into :class:`SubnetRecord` objects."""

    results: List[SubnetRecord] = []
    for subnet in subnets:
        if subnet.get("id") is None:
            logger.debug("Skipping subnet without identifier: %s", subnet)
            continue
        subnet_id = str(subnet.get("id"))

        try:
            subnet_value = decode_ip_value(subnet.get("subnet"))
            mask = int(subnet.get("mask"))
            network = ip_network(f"{subnet_value}/{mask}", strict=False)
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping subnet %s: %s", subnet_id, exc)
            continue

        customer_id = subnet.get("customer_id") or subnet.get("customerId")
        customer_id = str(customer_id) if customer_id not in (None, "", 0) else None

        record = SubnetRecord(
            id=subnet_id,
            prefix=str(network),
            description=(subnet.get("description") or None),
            customer_id=customer_id if customer_id in customer_index else None,
            is_pool=str(subnet.get("isPool", "0")) == "1",
            is_full=str(subnet.get("isFull", "0")) == "1",
            section_id=str(subnet.get("sectionId") or "") or None,
        )
        results.append(record)

    return results


def convert_addresses(
    addresses: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    subnets: Mapping[str, SubnetRecord],
    customer_index: Mapping[str, CustomerRecord],
    settings: ExportSettings,
) -> List[AddressRecord]:
    """Create :class:`AddressRecord` objects for all addresses."""

    results: List[AddressRecord] = []
    for subnet_id, address_list in addresses.items():
        subnet = subnets.get(str(subnet_id))
        if not subnet:
            logger.debug("Skipping addresses for unknown subnet %s", subnet_id)
            continue
        prefix_length = int(subnet.prefix.split("/")[1])

        for address in address_list:
            raw_ip = decode_ip_value(address.get("ip"))
            interface = ip_interface(f"{raw_ip}/{prefix_length}")

            customer_id = address.get("customer_id") or address.get("customerId")
            if not customer_id and subnet.customer_id:
                customer_id = subnet.customer_id
            customer_id = str(customer_id) if customer_id not in (None, "", 0) else None
            if customer_id not in customer_index:
                customer_id = None

            try:
                identifier = str(address.get("id")) if address.get("id") is not None else raw_ip
                record = AddressRecord(
                    id=identifier,
                    address=str(interface),
                    description=(address.get("description") or None),
                    hostname=(address.get("hostname") or address.get("dns_name") or None),
                    customer_id=customer_id,
                    tag=str(address.get("tag")) if address.get("tag") not in (None, "") else None,
                )
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping address %s in subnet %s: %s", address, subnet_id, exc)
                continue

            results.append(record)

    return results


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    """Write ``rows`` as a CSV file to ``path``."""

    if not rows:
        logger.info("Skipping %s because there are no rows to write", path.name)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

    logger.info("Wrote %s (%d rows)", path, len(rows))


def build_prefix_rows(
    subnets: Iterable[SubnetRecord],
    *,
    customers: Mapping[str, CustomerRecord],
    settings: ExportSettings,
) -> List[Dict[str, Any]]:
    """Return CSV rows for NetBox prefix imports."""

    rows: List[Dict[str, Any]] = []
    for subnet in subnets:
        row: Dict[str, Any] = {
            "prefix": subnet.prefix,
            "status": settings.prefix_status,
        }
        if subnet.description:
            row["description"] = subnet.description

        customer = customers.get(subnet.customer_id) if subnet.customer_id else None
        if settings.map_customer_to_tenant and customer:
            row["tenant"] = customer.name
            row["tenant__slug"] = customer.slug
            if settings.tenant_group:
                row["tenant_group"] = settings.tenant_group

        rows.append(row)

    return rows


def build_ip_rows(
    addresses: Iterable[AddressRecord],
    *,
    customers: Mapping[str, CustomerRecord],
    settings: ExportSettings,
) -> List[Dict[str, Any]]:
    """Return CSV rows for NetBox IP address imports."""

    rows: List[Dict[str, Any]] = []
    for address in addresses:
        row: Dict[str, Any] = {
            "address": address.address,
            "status": settings.address_status_for_tag(address.tag),
        }
        if address.hostname:
            row["dns_name"] = address.hostname
        if address.description:
            row["description"] = address.description

        customer = customers.get(address.customer_id) if address.customer_id else None
        if settings.map_customer_to_tenant and customer:
            row["tenant"] = customer.name
            row["tenant__slug"] = customer.slug
            if settings.tenant_group:
                row["tenant_group"] = settings.tenant_group

        rows.append(row)

    return rows


def build_tenant_rows(customers: Iterable[CustomerRecord]) -> List[Dict[str, Any]]:
    """Return CSV rows for NetBox tenant imports."""

    rows: List[Dict[str, Any]] = []
    for customer in customers:
        row: Dict[str, Any] = {
            "name": customer.name,
            "slug": customer.slug,
        }
        if customer.description:
            row["description"] = customer.description
        rows.append(row)
    return rows


def determine_used_customer_ids(
    subnets: Iterable[SubnetRecord], addresses: Iterable[AddressRecord]
) -> List[str]:
    """Return a sorted list of customer identifiers used in the export."""

    result = {subnet.customer_id for subnet in subnets if subnet.customer_id}
    result.update(address.customer_id for address in addresses if address.customer_id)
    return sorted(result)
