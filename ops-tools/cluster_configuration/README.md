# Cluster Configuration

`cluster_configuration.py` exports admin-level OpenStack configuration from a
running cloud and reapplies it after a rebuild. The generated inventory is
separate from the hardware/Kubespray inventory and defaults to
`cluster_configuration.yml`.

## Requirements

- `openstacksdk`
- `python-openstackclient`
- `ansible`
- `openstack.cloud` Ansible collection
- An admin-scoped `clouds.yaml` entry or OpenStack environment variables

## Export

```bash
ops-tools/cluster_configuration/cluster_configuration.py export \
  --cloud default \
  --output cluster_configuration.yml
```

The exporter uses `openstacksdk` for Nova, Cinder, and Neutron discovery where
possible. It uses bounded `openstack` CLI fallback calls for Octavia flavor
resources and service fields that are not exposed consistently through the SDK.

## Apply

Apply defaults to Ansible check mode:

```bash
ops-tools/cluster_configuration/cluster_configuration.py apply \
  --cloud default \
  --inventory cluster_configuration.yml
```

Perform changes explicitly:

```bash
ops-tools/cluster_configuration/cluster_configuration.py apply \
  --cloud default \
  --inventory cluster_configuration.yml \
  --apply
```

The apply path does not delete unmanaged resources. Existing resources are left
alone unless the underlying OpenStack module or command can safely update them.

## Inventory Schema

The inventory root is `openstack_cluster_configuration`.

Top-level resource lists:

- `flavors`: Nova compute flavors, visibility, access metadata, and extra specs.
- `volume_qos`: Cinder QoS specs.
- `volume_types`: Cinder volume types and extra specs.
- `octavia_flavor_profiles`: Octavia load balancer flavor profiles.
- `octavia_flavors`: Octavia load balancer flavors.
- `networks`: Neutron external/provider networks and their subnets.
- `routers`: routers with external gateway or route configuration.
- `network_qos_policies`: Neutron QoS policies.
- `security_groups`: non-default security groups and rules.

Resource identity is name-based for replay. Exported UUIDs are kept under
`metadata.source_id` only for operator traceability and are not treated as target
IDs for a rebuilt cloud.

## Safety Notes

- Destructive deletes are out of scope for v1.
- Tenant workload restoration is out of scope for v1.
- Subnets with more than one allocation pool fail validation because the
  current Ansible subnet module only supports one allocation pool.
- Secrets are not exported.

## Exit Codes

- `0`: command completed successfully.
- `1`: export, validation, credential, or runtime error.
- `3`: apply was attempted and Ansible returned a failure.

## Development

```bash
python3 -m unittest ops-tools/cluster_configuration/test_cluster_configuration.py
ansible-playbook --syntax-check ansible/playbooks/cluster_configuration_apply.yaml \
  -i localhost, \
  -e cluster_config_file=ops-tools/cluster_configuration/fixtures/minimal.yml
```
