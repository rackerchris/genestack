#!/usr/bin/env python3
"""Export and apply OpenStack cluster configuration inventory."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_APPLY_FAILED = 3

DEFAULT_INVENTORY = "cluster_configuration.yml"


class ToolError(RuntimeError):
    """Operator-facing error."""


def log(args: argparse.Namespace, message: str) -> None:
    if not getattr(args, "quiet", False):
        print(message, file=sys.stderr)


def load_yaml_file(path: Path) -> Any:
    try:
        from ruamel.yaml import YAML  # type: ignore

        yaml = YAML(typ="safe")
        with path.open(encoding="utf-8") as handle:
            return yaml.load(handle)
    except ImportError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ToolError("install ruamel.yaml or PyYAML to read YAML files") from exc
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)


def dump_yaml(data: Any) -> str:
    try:
        from ruamel.yaml import YAML  # type: ignore
        from ruamel.yaml.compat import StringIO  # type: ignore

        yaml = YAML()
        yaml.default_flow_style = False
        yaml.indent(mapping=2, sequence=4, offset=2)
        stream = StringIO()
        yaml.dump(data, stream)
        return stream.getvalue()
    except ImportError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ToolError("install ruamel.yaml or PyYAML to write YAML files") from exc
        return yaml.safe_dump(data, sort_keys=False)


def obj_get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
        if hasattr(obj, "get"):
            try:
                value = obj.get(name)
            except Exception:
                value = None
            if value is not None:
                return value
    return default


def clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def sorted_by_name(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get("name", "")))


def id_name_map(items: Iterable[Any]) -> dict[str, str]:
    mapping = {}
    for item in items:
        item_id = obj_get(item, "id")
        name = obj_get(item, "name")
        if item_id and name:
            mapping[str(item_id)] = str(name)
    return mapping


def is_uuid_like(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def run_command(command: list[str], timeout: int) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout}s: {' '.join(command)}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise ToolError(f"command failed ({completed.returncode}): {' '.join(command)}{suffix}")
    return completed.stdout


def run_openstack_json(
    args: argparse.Namespace, extra: list[str], required: bool = False
) -> Any:
    command = shlex.split(args.openstack_command) + extra + ["-f", "json"]
    try:
        stdout = run_command(command, args.command_timeout)
    except ToolError:
        if required:
            raise
        return [] if "list" in extra else {}
    try:
        return json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        if required:
            raise ToolError(f"failed to parse JSON from {' '.join(command)}: {exc}") from exc
        return [] if "list" in extra else {}


def maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def normalize_octavia_flavor_data(
    flavor_data: Any, flavor_names_by_id: dict[str, str]
) -> tuple[Any, dict[str, Any]]:
    data = maybe_json(flavor_data)
    metadata = {}
    if isinstance(data, dict):
        compute_flavor = data.get("compute_flavor")
        if compute_flavor and str(compute_flavor) in flavor_names_by_id:
            data = dict(data)
            metadata["source_compute_flavor_id"] = compute_flavor
            data["compute_flavor"] = flavor_names_by_id[str(compute_flavor)]
    return data, metadata


def sdk_connection(args: argparse.Namespace) -> Any:
    try:
        import openstack  # type: ignore
    except ImportError as exc:
        raise ToolError("openstacksdk is required for export") from exc
    try:
        conn = openstack.connect(cloud=args.cloud)
        conn.authorize()
        return conn
    except Exception as exc:
        raise ToolError(f"failed to authenticate to OpenStack cloud {args.cloud!r}: {exc}") from exc


def list_from_proxy(proxy: Any, *method_names: str) -> list[Any]:
    for method_name in method_names:
        method = getattr(proxy, method_name, None)
        if method is None:
            continue
        try:
            return list(method())
        except TypeError:
            try:
                return list(method(details=True))
            except TypeError:
                continue
        except Exception:
            continue
    return []


def export_flavors(conn: Any) -> list[dict[str, Any]]:
    flavors = []
    compute = getattr(conn, "compute", None)
    identity = getattr(conn, "identity", None)
    project_names_by_id = id_name_map(list_from_proxy(identity, "projects"))
    for flavor in list_from_proxy(compute, "flavors", "list_flavors"):
        extra_specs = obj_get(flavor, "extra_specs", "properties", default={}) or {}
        if compute is not None and not extra_specs:
            getter = getattr(compute, "get_flavor_extra_specs", None)
            if getter is not None:
                try:
                    extra_specs = getter(flavor)
                except Exception:
                    extra_specs = {}
        access = []
        access_getter = getattr(compute, "flavor_access", None) if compute is not None else None
        if access_getter is not None:
            try:
                access = [
                    obj_get(item, "tenant_id", "project_id", default=item)
                    for item in access_getter(flavor)
                ]
            except Exception:
                access = []
        access_project_ids = sorted([item for item in access if item])
        access_project_names = sorted(
            project_names_by_id[item] for item in access_project_ids if item in project_names_by_id
        )
        flavors.append(
            clean_dict(
                {
                    "name": obj_get(flavor, "name"),
                    "description": obj_get(flavor, "description"),
                    "ram": obj_get(flavor, "ram"),
                    "disk": obj_get(flavor, "disk"),
                    "vcpus": obj_get(flavor, "vcpus", "vcpu"),
                    "ephemeral": obj_get(flavor, "OS-FLV-EXT-DATA:ephemeral", "ephemeral", default=0),
                    "swap": obj_get(flavor, "swap", default=0),
                    "is_public": obj_get(flavor, "os-flavor-access:is_public", "is_public", default=True),
                    "extra_specs": dict(extra_specs),
                    "access_projects": access_project_names or None,
                    "metadata": clean_dict(
                        {
                            "source_id": obj_get(flavor, "id"),
                            "source_access_project_ids": access_project_ids or None,
                        }
                    ),
                }
            )
        )
    return sorted_by_name(flavors)


def export_volume_types(conn: Any) -> list[dict[str, Any]]:
    block = getattr(conn, "block_storage", None)
    volume_types = []
    for volume_type in list_from_proxy(block, "types", "volume_types"):
        extra_specs = obj_get(volume_type, "extra_specs", "properties", default={}) or {}
        qos = obj_get(volume_type, "qos_specs_id")
        volume_types.append(
            clean_dict(
                {
                    "name": obj_get(volume_type, "name"),
                    "description": obj_get(volume_type, "description"),
                    "is_public": obj_get(volume_type, "is_public", "public"),
                    "extra_specs": dict(extra_specs),
                    "qos_specs_id": qos,
                    "metadata": {"source_id": obj_get(volume_type, "id")},
                }
            )
        )
    return sorted_by_name(volume_types)


def export_volume_qos(conn: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    block = getattr(conn, "block_storage", None)
    qos_specs = []
    sdk_items = list_from_proxy(block, "qos_specs", "qos_specifications")
    if sdk_items:
        for item in sdk_items:
            qos_specs.append(
                clean_dict(
                    {
                        "name": obj_get(item, "name"),
                        "consumer": obj_get(item, "consumer"),
                        "specs": dict(obj_get(item, "specs", "properties", default={}) or {}),
                        "metadata": {"source_id": obj_get(item, "id")},
                    }
                )
            )
        return sorted_by_name(qos_specs)

    for row in run_openstack_json(args, ["volume", "qos", "list"]):
        qos_id = obj_get(row, "ID", "id")
        if not qos_id:
            continue
        detail = run_openstack_json(args, ["volume", "qos", "show", str(qos_id)])
        qos_specs.append(
            clean_dict(
                {
                    "name": obj_get(detail, "name", "Name"),
                    "consumer": obj_get(detail, "consumer", "Consumer"),
                    "specs": dict(obj_get(detail, "specs", "properties", "Properties", default={}) or {}),
                    "metadata": {"source_id": qos_id},
                }
            )
        )
    return sorted_by_name(qos_specs)


def normalize_qos_rules(rules: Iterable[Any]) -> list[dict[str, Any]]:
    normalized = []
    for rule in rules or []:
        rule_type = obj_get(rule, "type")
        if not rule_type:
            rule_type = obj_get(rule, "rule_type")
        normalized.append(
            clean_dict(
                {
                    "type": rule_type,
                    "direction": obj_get(rule, "direction"),
                    "max_kbps": obj_get(rule, "max_kbps"),
                    "max_burst_kbits": obj_get(rule, "max_burst_kbits"),
                    "dscp_mark": obj_get(rule, "dscp_mark"),
                    "min_kbps": obj_get(rule, "min_kbps"),
                    "metadata": clean_dict({"source_id": obj_get(rule, "id")}),
                }
            )
        )
    return normalized


def export_networks(
    conn: Any,
    qos_names_by_id: dict[str, str] | None = None,
    project_names_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    network = getattr(conn, "network", None)
    qos_names_by_id = qos_names_by_id or {}
    project_names_by_id = project_names_by_id or {}
    subnets_by_id = {
        obj_get(subnet, "id"): subnet for subnet in list_from_proxy(network, "subnets")
    }
    results = []
    for net in list_from_proxy(network, "networks"):
        provider_type = obj_get(net, "provider_network_type", "provider:network_type")
        provider_physnet = obj_get(net, "provider_physical_network", "provider:physical_network")
        external = obj_get(net, "is_router_external", "router:external", default=False)
        shared = obj_get(net, "is_shared", "shared", default=False)
        if not (external or shared or provider_physnet):
            continue
        subnet_items = []
        for subnet_id in obj_get(net, "subnet_ids", "subnets", default=[]) or []:
            subnet = subnets_by_id.get(subnet_id)
            if subnet is None:
                continue
            subnet_items.append(
                clean_dict(
                    {
                        "name": obj_get(subnet, "name"),
                        "cidr": obj_get(subnet, "cidr"),
                        "gateway_ip": obj_get(subnet, "gateway_ip"),
                        "enable_dhcp": obj_get(subnet, "is_dhcp_enabled", "enable_dhcp"),
                        "allocation_pools": obj_get(subnet, "allocation_pools", default=[]),
                        "dns_nameservers": obj_get(subnet, "dns_nameservers", default=[]),
                        "host_routes": obj_get(subnet, "host_routes", default=[]),
                        "service_types": obj_get(subnet, "service_types", default=[]),
                        "metadata": {"source_id": obj_get(subnet, "id")},
                    }
                )
            )
        qos_policy_id = obj_get(net, "qos_policy_id")
        project_id = obj_get(net, "project_id", "tenant_id")
        results.append(
            clean_dict(
                {
                    "name": obj_get(net, "name"),
                    "project": project_names_by_id.get(str(project_id)) if project_id else None,
                    "provider_physical_network": provider_physnet,
                    "provider_network_type": provider_type,
                    "provider_segmentation_id": obj_get(net, "provider_segmentation_id", "provider:segmentation_id"),
                    "external": external,
                    "shared": shared,
                    "mtu": obj_get(net, "mtu"),
                    "qos_policy": qos_names_by_id.get(str(qos_policy_id)) if qos_policy_id else None,
                    "subnets": sorted_by_name(subnet_items),
                    "metadata": clean_dict(
                        {
                            "source_id": obj_get(net, "id"),
                            "source_project_id": project_id,
                            "source_qos_policy_id": qos_policy_id,
                        }
                    ),
                }
            )
        )
    return sorted_by_name(results)


def export_routers(conn: Any, project_names_by_id: dict[str, str] | None = None) -> list[dict[str, Any]]:
    network = getattr(conn, "network", None)
    project_names_by_id = project_names_by_id or {}
    network_names_by_id = id_name_map(list_from_proxy(network, "networks"))
    subnet_names_by_id = id_name_map(list_from_proxy(network, "subnets"))
    routers = []
    for router in list_from_proxy(network, "routers"):
        gateway_info = obj_get(router, "external_gateway_info")
        gateway_metadata = {}
        if isinstance(gateway_info, dict) and gateway_info.get("network_id"):
            gateway_metadata["source_network_id"] = gateway_info.get("network_id")
            gateway_info = dict(gateway_info)
            network_id = gateway_info.pop("network_id")
            gateway_info["network_name"] = network_names_by_id.get(str(network_id))
            fixed_ips = []
            for fixed_ip in gateway_info.get("external_fixed_ips", []) or []:
                fixed_ip = dict(fixed_ip)
                subnet_id = fixed_ip.pop("subnet_id", None)
                if subnet_id:
                    fixed_ip["subnet"] = subnet_names_by_id.get(str(subnet_id))
                    fixed_ip["metadata"] = {"source_subnet_id": subnet_id}
                fixed_ips.append(clean_dict(fixed_ip))
            if fixed_ips:
                gateway_info["external_fixed_ips"] = fixed_ips
        if not gateway_info and not obj_get(router, "routes"):
            continue
        project_id = obj_get(router, "project_id", "tenant_id")
        project_name = project_names_by_id.get(str(project_id)) if project_id else None
        if project_id and (not project_name or is_uuid_like(project_name)):
            continue
        routers.append(
            clean_dict(
                {
                    "name": obj_get(router, "name"),
                    "project": project_name,
                    "external_gateway_info": gateway_info,
                    "routes": obj_get(router, "routes", default=[]),
                    "metadata": clean_dict(
                        {"source_id": obj_get(router, "id"), "source_project_id": project_id, **gateway_metadata}
                    ),
                }
            )
        )
    return sorted_by_name(routers)


def export_network_qos_policies(conn: Any) -> list[dict[str, Any]]:
    network = getattr(conn, "network", None)
    policies = []
    for policy in list_from_proxy(network, "qos_policies"):
        policies.append(
            clean_dict(
                {
                    "name": obj_get(policy, "name"),
                    "description": obj_get(policy, "description"),
                    "shared": obj_get(policy, "is_shared", "shared"),
                    "is_default": obj_get(policy, "is_default"),
                    "rules": normalize_qos_rules(obj_get(policy, "rules", default=[])),
                    "metadata": {"source_id": obj_get(policy, "id")},
                }
            )
        )
    return sorted_by_name(policies)


def export_security_groups(conn: Any, project_names_by_id: dict[str, str] | None = None) -> list[dict[str, Any]]:
    network = getattr(conn, "network", None)
    project_names_by_id = project_names_by_id or {}
    raw_groups = list_from_proxy(network, "security_groups")
    group_names_by_id = {
        str(obj_get(group, "id")): obj_get(group, "name") for group in raw_groups if obj_get(group, "id")
    }
    groups = []
    for group in raw_groups:
        name = obj_get(group, "name")
        if name == "default":
            continue
        project_id = obj_get(group, "project_id", "tenant_id")
        project_name = project_names_by_id.get(str(project_id)) if project_id else None
        if project_id and (not project_name or is_uuid_like(project_name)):
            continue
        rules = []
        for rule in obj_get(group, "security_group_rules", "rules", default=[]) or []:
            rule_dict = {
                key: obj_get(rule, key)
                for key in (
                    "direction",
                    "protocol",
                    "port_range_min",
                    "port_range_max",
                    "ethertype",
                    "remote_ip_prefix",
                    "remote_group_id",
                )
            }
            remote_group_id = rule_dict.get("remote_group_id")
            if remote_group_id and str(remote_group_id) in group_names_by_id:
                rule_dict["remote_group"] = group_names_by_id[str(remote_group_id)]
                rule_dict.pop("remote_group_id", None)
            rule_dict["metadata"] = clean_dict({"source_id": obj_get(rule, "id")})
            rules.append(clean_dict(rule_dict))
        groups.append(
            clean_dict(
                {
                    "name": name,
                    "project": project_name,
                    "description": obj_get(group, "description"),
                    "rules": rules,
                    "metadata": clean_dict({"source_id": obj_get(group, "id"), "source_project_id": project_id}),
                }
            )
        )
    return sorted_by_name(groups)


def export_octavia(
    conn: Any, args: argparse.Namespace, flavor_names_by_id: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    load_balancer = getattr(conn, "load_balancer", None)
    profiles = []
    sdk_profiles = list_from_proxy(load_balancer, "flavor_profiles")
    if sdk_profiles:
        for profile in sdk_profiles:
            flavor_data, flavor_data_metadata = normalize_octavia_flavor_data(
                obj_get(profile, "flavor_data"), flavor_names_by_id
            )
            profiles.append(
                clean_dict(
                    {
                        "name": obj_get(profile, "name"),
                        "provider": obj_get(profile, "provider_name", "provider"),
                        "flavor_data": flavor_data,
                        "metadata": clean_dict({"source_id": obj_get(profile, "id"), **flavor_data_metadata}),
                    }
                )
            )
    else:
        for row in run_openstack_json(args, ["loadbalancer", "flavorprofile", "list"]):
            profile_id = obj_get(row, "id", "ID")
            if not profile_id:
                continue
            detail = run_openstack_json(args, ["loadbalancer", "flavorprofile", "show", str(profile_id)])
            flavor_data, flavor_data_metadata = normalize_octavia_flavor_data(
                obj_get(detail, "flavor_data", "Flavor Data"), flavor_names_by_id
            )
            profiles.append(
                clean_dict(
                    {
                        "name": obj_get(detail, "name", "Name"),
                        "provider": obj_get(detail, "provider_name", "provider", "Provider"),
                        "flavor_data": flavor_data,
                        "metadata": clean_dict({"source_id": profile_id, **flavor_data_metadata}),
                    }
                )
            )

    profile_names_by_id = {
        item.get("metadata", {}).get("source_id"): item.get("name") for item in profiles
    }
    flavors = []
    sdk_flavors = list_from_proxy(load_balancer, "flavors")
    if sdk_flavors:
        for flavor in sdk_flavors:
            profile_id = obj_get(flavor, "flavor_profile_id", "flavorprofile_id")
            flavors.append(
                clean_dict(
                    {
                        "name": obj_get(flavor, "name"),
                        "description": obj_get(flavor, "description"),
                        "enabled": obj_get(flavor, "is_enabled", "enabled", default=True),
                        "flavor_profile": profile_names_by_id.get(profile_id, profile_id),
                        "metadata": clean_dict(
                            {"source_id": obj_get(flavor, "id"), "source_flavor_profile_id": profile_id}
                        ),
                    }
                )
            )
    else:
        for row in run_openstack_json(args, ["loadbalancer", "flavor", "list"]):
            flavor_id = obj_get(row, "id", "ID")
            if not flavor_id:
                continue
            detail = run_openstack_json(args, ["loadbalancer", "flavor", "show", str(flavor_id)])
            profile_id = obj_get(detail, "flavor_profile_id", "flavorprofile_id", "Flavor Profile ID")
            flavors.append(
                clean_dict(
                    {
                        "name": obj_get(detail, "name", "Name"),
                        "description": obj_get(detail, "description", "Description"),
                        "enabled": obj_get(detail, "enabled", "Enabled", default=True),
                        "flavor_profile": profile_names_by_id.get(profile_id, profile_id),
                        "metadata": clean_dict({"source_id": flavor_id, "source_flavor_profile_id": profile_id}),
                    }
                )
            )
    return sorted_by_name(profiles), sorted_by_name(flavors)


def build_inventory(conn: Any, args: argparse.Namespace) -> dict[str, Any]:
    log(args, "Exporting Nova flavors")
    flavors = export_flavors(conn)
    flavor_names_by_id = {
        item.get("metadata", {}).get("source_id"): item.get("name") for item in flavors
    }
    log(args, "Exporting Cinder volume types and QoS")
    volume_types = export_volume_types(conn)
    volume_qos = export_volume_qos(conn, args)
    volume_qos_names_by_id = {
        item.get("metadata", {}).get("source_id"): item.get("name") for item in volume_qos
    }
    for volume_type in volume_types:
        qos_id = volume_type.pop("qos_specs_id", None)
        if qos_id and qos_id in volume_qos_names_by_id:
            volume_type["qos"] = volume_qos_names_by_id[qos_id]
    network_qos = export_network_qos_policies(conn)
    network_qos_names_by_id = {
        item.get("metadata", {}).get("source_id"): item.get("name") for item in network_qos
    }
    identity = getattr(conn, "identity", None)
    project_names_by_id = id_name_map(list_from_proxy(identity, "projects"))
    log(args, "Exporting Neutron provider resources")
    networks = export_networks(conn, network_qos_names_by_id, project_names_by_id)
    routers = export_routers(conn, project_names_by_id)
    security_groups = export_security_groups(conn, project_names_by_id)
    log(args, "Exporting Octavia flavors")
    octavia_profiles, octavia_flavors = export_octavia(conn, args, flavor_names_by_id)

    return {
        "openstack_cluster_configuration": {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cloud": args.cloud,
            "flavors": flavors,
            "volume_qos": volume_qos,
            "volume_types": volume_types,
            "octavia_flavor_profiles": octavia_profiles,
            "octavia_flavors": octavia_flavors,
            "networks": networks,
            "routers": routers,
            "network_qos_policies": network_qos,
            "security_groups": security_groups,
        }
    }


def validate_named_list(config: dict[str, Any], key: str) -> list[str]:
    errors = []
    seen: dict[str, int] = {}
    for index, item in enumerate(config.get(key, []) or [], start=1):
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            errors.append(f"{key}[{index}] is missing required name")
            continue
        scope = item.get("project") if isinstance(item, dict) else None
        identity = f"{scope}/{name}" if scope else name
        if identity in seen:
            errors.append(f"{key}[{index}] duplicates name {identity!r} from item {seen[identity]}")
        seen[identity] = index
    return errors


def validate_inventory(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ToolError("inventory must be a YAML mapping")
    config = data.get("openstack_cluster_configuration")
    if not isinstance(config, dict):
        raise ToolError("inventory is missing openstack_cluster_configuration mapping")

    errors: list[str] = []
    for key in (
        "flavors",
        "volume_qos",
        "volume_types",
        "octavia_flavor_profiles",
        "octavia_flavors",
        "networks",
        "routers",
        "network_qos_policies",
        "security_groups",
    ):
        errors.extend(validate_named_list(config, key))

    qos_names = {item["name"] for item in config.get("volume_qos", []) or [] if item.get("name")}
    for item in config.get("volume_types", []) or []:
        qos_name = item.get("qos")
        if qos_name and qos_name not in qos_names:
            errors.append(f"volume_types[{item.get('name')}] references unknown qos {qos_name!r}")

    profile_names = {
        item["name"] for item in config.get("octavia_flavor_profiles", []) or [] if item.get("name")
    }
    for item in config.get("octavia_flavors", []) or []:
        profile = item.get("flavor_profile")
        if profile and profile in profile_names:
            continue
        if profile:
            errors.append(f"octavia_flavors[{item.get('name')}] references unknown flavor_profile {profile!r}")
    flavor_names = {item["name"] for item in config.get("flavors", []) or [] if item.get("name")}
    for item in config.get("octavia_flavor_profiles", []) or []:
        flavor_data = item.get("flavor_data")
        if isinstance(flavor_data, dict) and flavor_data.get("compute_flavor"):
            compute_flavor = flavor_data["compute_flavor"]
            if is_uuid_like(compute_flavor):
                errors.append(
                    f"octavia_flavor_profiles[{item.get('name')}] still references old compute_flavor UUID"
                )
            elif compute_flavor not in flavor_names:
                errors.append(
                    f"octavia_flavor_profiles[{item.get('name')}] references unknown compute_flavor {compute_flavor!r}"
                )

    network_qos_names = {
        item["name"] for item in config.get("network_qos_policies", []) or [] if item.get("name")
    }
    for network in config.get("networks", []) or []:
        qos_policy = network.get("qos_policy")
        if qos_policy and qos_policy not in network_qos_names:
            errors.append(f"networks[{network.get('name')}] references unknown qos_policy {qos_policy!r}")
        for subnet in network.get("subnets", []) or []:
            pools = subnet.get("allocation_pools", []) or []
            if len(pools) > 1:
                errors.append(
                    f"subnet {subnet.get('name')!r} on network {network.get('name')!r} has multiple allocation pools"
                )

    if errors:
        raise ToolError("inventory validation failed:\n  - " + "\n  - ".join(errors))
    return config


def write_inventory(data: dict[str, Any], args: argparse.Namespace) -> None:
    if args.format == "json":
        content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    else:
        content = dump_yaml(data)
    if args.output and args.output != "-":
        Path(args.output).write_text(content, encoding="utf-8")
        log(args, f"Wrote {args.output}")
    else:
        print(content, end="")


def command_export(args: argparse.Namespace) -> int:
    conn = sdk_connection(args)
    inventory = build_inventory(conn, args)
    validate_inventory(inventory)
    write_inventory(inventory, args)
    return EXIT_OK


def playbook_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ansible" / "playbooks" / "cluster_configuration_apply.yaml"


def command_apply(args: argparse.Namespace) -> int:
    inventory_path = Path(args.inventory)
    data = load_yaml_file(inventory_path)
    validate_inventory(data)
    command = [
        args.ansible_playbook_command,
        str(playbook_path()),
        "-i",
        "localhost,",
        "-e",
        f"cluster_config_file={inventory_path.resolve()}",
        "-e",
        f"os_cloud={args.cloud}",
    ]
    if not args.apply:
        command.append("--check")
    log(args, f"Running: {' '.join(command)}")
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        fallback = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "ansible-playbook"
        if args.ansible_playbook_command == "ansible-playbook" and fallback.exists():
            command[0] = str(fallback)
            completed = subprocess.run(command, check=False)
        else:
            raise ToolError(f"command not found: {args.ansible_playbook_command}")
    return EXIT_OK if completed.returncode == 0 else EXIT_APPLY_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="export active cloud configuration")
    export.add_argument("--cloud", default=os.environ.get("OS_CLOUD", "default"))
    export.add_argument("--output", "-o", default=DEFAULT_INVENTORY)
    export.add_argument("--format", choices=("yaml", "json"), default="yaml")
    export.add_argument("--quiet", action="store_true")
    export.add_argument("--command-timeout", type=int, default=60)
    export.add_argument("--openstack-command", default="openstack")
    export.set_defaults(func=command_export)

    apply = subparsers.add_parser("apply", help="apply exported cluster configuration")
    apply.add_argument("--cloud", default=os.environ.get("OS_CLOUD", "default"))
    apply.add_argument("--inventory", "-i", default=DEFAULT_INVENTORY)
    apply.add_argument("--apply", action="store_true", help="perform changes; default is Ansible check mode")
    apply.add_argument("--quiet", action="store_true")
    apply.add_argument("--ansible-playbook-command", default="ansible-playbook")
    apply.set_defaults(func=command_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
