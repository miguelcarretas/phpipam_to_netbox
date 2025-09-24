import unittest

from phpipam_to_netbox.converters import (
    ExportSettings,
    build_customer_records,
    build_ip_rows,
    build_prefix_rows,
    build_tenant_rows,
    convert_addresses,
    convert_subnets,
    decode_ip_value,
    determine_used_customer_ids,
    slugify,
)


class ConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.customers_raw = [
            {"id": 1, "title": "Cliente Uno", "description": "Cliente principal"},
            {"id": 2, "title": "Cliente Dos", "note": "Cliente secundario"},
        ]
        self.subnets_raw = [
            {
                "id": 10,
                "subnet": "10.0.0.0",
                "mask": 24,
                "description": "LAN",
                "customer_id": 1,
                "isFolder": "0",
                "isPool": "0",
                "isFull": "0",
            },
            {
                "id": 11,
                "subnet": "2001:db8::",
                "mask": 64,
                "description": "IPv6",
                "customer_id": 2,
            },
        ]
        self.addresses_raw = {
            "10": [
                {
                    "id": 100,
                    "ip": "10.0.0.5",
                    "description": "Servidor",
                    "hostname": "srv01",
                    "tag": 1,
                },
                {
                    "id": 101,
                    "ip": "10.0.0.10",
                    "description": "Reservada",
                    "tag": 2,
                },
            ],
            "11": [
                {
                    "id": 200,
                    "ip": "2001:db8::1",
                    "hostname": "router",
                    "tag": 3,
                }
            ],
        }

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Cliente Uno"), "cliente-uno")
        self.assertEqual(slugify(" Cliente  Uno "), "cliente-uno")
        self.assertEqual(slugify("áéí"), "tenant")

    def test_decode_ip_value(self) -> None:
        self.assertEqual(decode_ip_value("10.0.0.1"), "10.0.0.1")
        self.assertEqual(decode_ip_value("3232235521"), "192.168.0.1")
        with self.assertRaises(ValueError):
            decode_ip_value(None)

    def test_conversion_pipeline(self) -> None:
        customers, customer_index = build_customer_records(self.customers_raw)
        settings = ExportSettings()

        subnets = convert_subnets(self.subnets_raw, customer_index=customer_index, settings=settings)
        subnet_index = {s.id: s for s in subnets}

        addresses = convert_addresses(
            self.addresses_raw,
            subnets=subnet_index,
            customer_index=customer_index,
            settings=settings,
        )

        used = determine_used_customer_ids(subnets, addresses)
        self.assertEqual(used, ["1", "2"])

        tenant_rows = build_tenant_rows(customers)
        self.assertEqual(len(tenant_rows), 2)

        prefix_rows = build_prefix_rows(subnets, customers=customer_index, settings=settings)
        self.assertEqual(prefix_rows[0]["prefix"], "10.0.0.0/24")
        self.assertEqual(prefix_rows[0]["tenant"], "Cliente Uno")

        ip_rows = build_ip_rows(addresses, customers=customer_index, settings=settings)
        self.assertTrue(any(row["tenant"] == "Cliente Uno" for row in ip_rows))
        statuses = {row["status"] for row in ip_rows}
        self.assertIn("active", statuses)
        self.assertIn("reserved", statuses)


if __name__ == "__main__":
    unittest.main()
