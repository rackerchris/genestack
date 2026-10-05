#!/usr/bin/env python3
"""Unit tests for cluster_configuration.py."""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import cluster_configuration as tool


class ClusterConfigurationTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            cloud="default",
            quiet=True,
            command_timeout=60,
            openstack_command="openstack",
            format="yaml",
            output=None,
        )

    def test_export_flavors_normalizes_sdk_resources(self) -> None:
        conn = SimpleNamespace(
            compute=SimpleNamespace(
                flavors=lambda: [
                    SimpleNamespace(
                        id="flavor-id",
                        name="gp.0.1.2",
                        description="general",
                        ram=2048,
                        disk=10,
                        vcpus=1,
                        ephemeral=0,
                        swap=0,
                        is_public=True,
                        extra_specs={"hw:mem_page_size": "any"},
                    )
                ]
            ),
            identity=SimpleNamespace(projects=lambda: []),
        )

        self.assertEqual(
            tool.export_flavors(conn),
            [
                {
                    "name": "gp.0.1.2",
                    "description": "general",
                    "ram": 2048,
                    "disk": 10,
                    "vcpus": 1,
                    "ephemeral": 0,
                    "swap": 0,
                    "is_public": True,
                    "extra_specs": {"hw:mem_page_size": "any"},
                    "metadata": {"source_id": "flavor-id"},
                }
            ],
        )

    def test_export_volume_qos_uses_cli_fallback(self) -> None:
        responses = {
            ("volume", "qos", "list"): [{"ID": "qos-id"}],
            ("volume", "qos", "show", "qos-id"): {
                "name": "Standard-Block",
                "consumer": "both",
                "specs": {"read_iops_sec_per_gb": "5"},
            },
        }

        def fake_json(args: argparse.Namespace, extra: list[str], required: bool = False):
            del args, required
            return responses[tuple(extra)]

        with mock.patch.object(tool, "run_openstack_json", side_effect=fake_json):
            self.assertEqual(
                tool.export_volume_qos(SimpleNamespace(block_storage=SimpleNamespace()), self.args()),
                [
                    {
                        "name": "Standard-Block",
                        "consumer": "both",
                        "specs": {"read_iops_sec_per_gb": "5"},
                        "metadata": {"source_id": "qos-id"},
                    }
                ],
            )

    def test_export_octavia_uses_cli_fallback(self) -> None:
        responses = {
            ("loadbalancer", "flavorprofile", "list"): [{"ID": "profile-id"}],
            ("loadbalancer", "flavorprofile", "show", "profile-id"): {
                "name": "fp.single",
                "provider": "amphora",
                "flavor_data": {"loadbalancer_topology": "SINGLE"},
            },
            ("loadbalancer", "flavor", "list"): [{"ID": "flavor-id"}],
            ("loadbalancer", "flavor", "show", "flavor-id"): {
                "name": "single",
                "description": "single amphora",
                "enabled": True,
                "flavor_profile_id": "profile-id",
            },
        }

        def fake_json(args: argparse.Namespace, extra: list[str], required: bool = False):
            del args, required
            return responses[tuple(extra)]

        with mock.patch.object(tool, "run_openstack_json", side_effect=fake_json):
            profiles, flavors = tool.export_octavia(
                SimpleNamespace(load_balancer=SimpleNamespace()), self.args(), {}
            )

        self.assertEqual(profiles[0]["name"], "fp.single")
        self.assertEqual(flavors[0]["name"], "single")
        self.assertEqual(flavors[0]["flavor_profile"], "fp.single")
        self.assertEqual(flavors[0]["metadata"]["source_flavor_profile_id"], "profile-id")

    def test_export_octavia_sdk_maps_compute_flavor_dependency(self) -> None:
        conn = SimpleNamespace(
            load_balancer=SimpleNamespace(
                flavor_profiles=lambda: [
                    SimpleNamespace(
                        id="profile-id",
                        name="fp.single",
                        provider_name="amphora",
                        flavor_data='{"loadbalancer_topology": "SINGLE", "compute_flavor": "flavor-id"}',
                    )
                ],
                flavors=lambda: [
                    SimpleNamespace(
                        id="octavia-flavor-id",
                        name="single",
                        description="single amphora",
                        is_enabled=True,
                        flavor_profile_id="profile-id",
                    )
                ],
            )
        )

        profiles, flavors = tool.export_octavia(conn, self.args(), {"flavor-id": "amphora.vm"})

        self.assertEqual(profiles[0]["flavor_data"]["compute_flavor"], "amphora.vm")
        self.assertEqual(profiles[0]["metadata"]["source_compute_flavor_id"], "flavor-id")
        self.assertEqual(flavors[0]["flavor_profile"], "fp.single")

    def test_export_networks_filters_to_provider_resources(self) -> None:
        subnet = SimpleNamespace(
            id="subnet-id",
            name="public-subnet",
            cidr="192.0.2.0/24",
            gateway_ip="192.0.2.1",
            is_dhcp_enabled=False,
            allocation_pools=[{"start": "192.0.2.10", "end": "192.0.2.20"}],
        )
        provider = SimpleNamespace(
            id="network-id",
            name="PUBLICNET",
            subnet_ids=["subnet-id"],
            provider_network_type="flat",
            provider_physical_network="physnet1",
            is_router_external=True,
            is_shared=True,
            qos_policy_id="qos-id",
        )
        tenant = SimpleNamespace(id="tenant-net", name="tenant", subnet_ids=[])
        conn = SimpleNamespace(
            network=SimpleNamespace(networks=lambda: [provider, tenant], subnets=lambda: [subnet])
        )

        networks = tool.export_networks(conn, {"qos-id": "gold-qos"})

        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0]["name"], "PUBLICNET")
        self.assertEqual(networks[0]["qos_policy"], "gold-qos")
        self.assertEqual(networks[0]["subnets"][0]["name"], "public-subnet")

    def test_export_flavor_access_uses_project_names(self) -> None:
        flavor = SimpleNamespace(
            id="flavor-id",
            name="private.tiny",
            ram=512,
            disk=1,
            vcpus=1,
            is_public=False,
            extra_specs={},
        )
        conn = SimpleNamespace(
            compute=SimpleNamespace(
                flavors=lambda: [flavor],
                flavor_access=lambda item: [SimpleNamespace(project_id="project-id")],
            ),
            identity=SimpleNamespace(projects=lambda: [SimpleNamespace(id="project-id", name="service")]),
        )

        exported = tool.export_flavors(conn)[0]

        self.assertEqual(exported["access_projects"], ["service"])
        self.assertEqual(exported["metadata"]["source_access_project_ids"], ["project-id"])

    def test_validate_inventory_rejects_duplicate_names_and_multi_pool_subnet(self) -> None:
        data = {
            "openstack_cluster_configuration": {
                "flavors": [{"name": "tiny"}, {"name": "tiny"}],
                "networks": [
                    {
                        "name": "PUBLICNET",
                        "subnets": [
                            {
                                "name": "public-subnet",
                                "allocation_pools": [
                                    {"start": "192.0.2.10", "end": "192.0.2.20"},
                                    {"start": "192.0.2.30", "end": "192.0.2.40"},
                                ],
                            }
                        ],
                    }
                ],
            }
        }

        with self.assertRaises(tool.ToolError) as raised:
            tool.validate_inventory(data)

        self.assertIn("duplicates name", str(raised.exception))
        self.assertIn("multiple allocation pools", str(raised.exception))

    def test_apply_defaults_to_check_mode(self) -> None:
        data = {"openstack_cluster_configuration": {"flavors": []}}
        with tempfile.NamedTemporaryFile("w", suffix=".yml") as handle:
            handle.write(tool.dump_yaml(data))
            handle.flush()
            args = argparse.Namespace(
                inventory=handle.name,
                cloud="default",
                apply=False,
                quiet=True,
                ansible_playbook_command="ansible-playbook",
            )
            with mock.patch.object(tool, "subprocess") as mocked_subprocess:
                mocked_subprocess.run.return_value.returncode = 0
                self.assertEqual(tool.command_apply(args), tool.EXIT_OK)

        command = mocked_subprocess.run.call_args.args[0]
        self.assertIn("--check", command)

    def test_apply_flag_removes_check_mode(self) -> None:
        data = {"openstack_cluster_configuration": {"flavors": []}}
        with tempfile.NamedTemporaryFile("w", suffix=".yml") as handle:
            handle.write(tool.dump_yaml(data))
            handle.flush()
            args = argparse.Namespace(
                inventory=handle.name,
                cloud="default",
                apply=True,
                quiet=True,
                ansible_playbook_command="ansible-playbook",
            )
            with mock.patch.object(tool, "subprocess") as mocked_subprocess:
                mocked_subprocess.run.return_value.returncode = 0
                self.assertEqual(tool.command_apply(args), tool.EXIT_OK)

        command = mocked_subprocess.run.call_args.args[0]
        self.assertNotIn("--check", command)

    def test_export_reports_credential_failure(self) -> None:
        with mock.patch.object(
            tool, "sdk_connection", side_effect=tool.ToolError("no auth")
        ), mock.patch("sys.stderr"):
            self.assertEqual(tool.main(["export", "--output", "-"]), tool.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
