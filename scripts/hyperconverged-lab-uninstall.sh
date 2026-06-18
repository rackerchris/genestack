#!/usr/bin/env bash
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

export LAB_NAME_PREFIX="${LAB_NAME_PREFIX:-hyperconverged}"

source "${SCRIPT_DIR}/lib/helpers.sh"
source "${SCRIPT_DIR}/lib/hyperconverged/ssh-keys.sh"
source "${SCRIPT_DIR}/lib/hyperconverged-uninstall-common.sh"

# Accept platform as first arg or prompt
if [ -n "$1" ]; then
    PLATFORM="$1"
else
    echo ""
    echo "Hyperconverged Lab Uninstall"
    echo "============================="
    echo ""
    echo "Select platform to uninstall:"
    echo ""
    echo "  1) Kubespray — Ubuntu VMs + Kubespray"
    echo "  2) Talos — Talos Linux + talosctl"
    echo ""
    read -rp "Enter choice [1/2]: " choice
    case "${choice:-1}" in
        1|kubespray) PLATFORM="kubespray" ;;
        2|talos)     PLATFORM="talos" ;;
        *)           echo "Invalid choice."; exit 1 ;;
    esac
fi

echo ""
echo "Uninstalling ${PLATFORM} lab..."
echo ""

if [ -z "${OS_CLOUD}" ]; then
    read -rp "Enter name of the cloud configuration used for this uninstall [default]: " OS_CLOUD || true
    export OS_CLOUD="${OS_CLOUD:-default}"
fi

configure_ssh_key_paths

_log STEP "Phase 1: Initialize uninstall inputs"
_log INFO "Uninstall inputs:"
_log INFO "  Platform: ${PLATFORM}"
_log INFO "  OS_CLOUD: ${OS_CLOUD}"
_log INFO "  LAB_NAME_PREFIX: ${LAB_NAME_PREFIX}"
_log INFO "  HYPERCONVERGED_SSH_KEY_PATH: ${HYPERCONVERGED_SSH_KEY_PATH:-}"
_log INFO "  HYPERCONVERGED_SSH_PUB_KEY_PATH: ${HYPERCONVERGED_SSH_PUB_KEY_PATH:-}"
_log INFO "  HYPERCONVERGED_CINDER_VOLUME: ${HYPERCONVERGED_CINDER_VOLUME:-false}"

_log STEP "Starting ${PLATFORM} uninstall"

# Delete floating IPs before server teardown so Nova-owned worker ports are
# still discoverable when the floating IP is attached.
_log INFO "Deleting worker 0 floating IP"
WORKER_0_PORT="$(_discover_server_port_on_network "${LAB_NAME_PREFIX}-0" "${LAB_NAME_PREFIX}-net")"
if [ -z "${WORKER_0_PORT}" ] && openstack port show ${LAB_NAME_PREFIX}-0-mgmt-port -f value -c id >/dev/null 2>&1; then
    WORKER_0_PORT=$(openstack port show ${LAB_NAME_PREFIX}-0-mgmt-port -f value -c id 2>/dev/null)
fi
if [ -n "${WORKER_0_PORT}" ] && FIP_ID=$(openstack floating ip list --port ${WORKER_0_PORT} -f value -c ID 2>/dev/null); then
    openstack floating ip delete ${FIP_ID} >/dev/null 2>&1 || true
fi

if [ "${PLATFORM}" = "talos" ]; then
    _log INFO "Deleting Talos jump host floating IP"
    JUMP_HOST_PORT="$(_discover_server_port_on_network "${LAB_NAME_PREFIX}-jump" "${LAB_NAME_PREFIX}-net")"
    if [ -z "${JUMP_HOST_PORT}" ] && openstack port show ${LAB_NAME_PREFIX}-jump-mgmt-port -f value -c id >/dev/null 2>&1; then
        JUMP_HOST_PORT=$(openstack port show ${LAB_NAME_PREFIX}-jump-mgmt-port -f value -c id 2>/dev/null)
    fi
    if [ -n "${JUMP_HOST_PORT}" ] && FIP_ID=$(openstack floating ip list --port ${JUMP_HOST_PORT} -f value -c ID 2>/dev/null); then
        openstack floating ip delete ${FIP_ID} >/dev/null 2>&1 || true
    fi
fi

# Delete MetalLB floating IP
_log INFO "Deleting MetalLB floating IP"
if openstack port show ${LAB_NAME_PREFIX}-metallb-vip-0-port -f value -c id >/dev/null 2>&1; then
    METAL_LB_PORT_ID=$(openstack port show ${LAB_NAME_PREFIX}-metallb-vip-0-port -f value -c id)
    if FIP_ID=$(openstack floating ip list --port ${METAL_LB_PORT_ID} -f value -c ID 2>/dev/null); then
        openstack floating ip delete ${FIP_ID} >/dev/null 2>&1 || true
    fi
fi

# Delete servers (skip VM setup for kubespray, only servers exist)
_log INFO "Deleting servers"
for i in 0 1 2; do
    if openstack server show ${LAB_NAME_PREFIX}-${i} -f value -c status >/dev/null 2>&1; then
        _log INFO "  Deleting server ${LAB_NAME_PREFIX}-${i}"
        openstack server delete ${LAB_NAME_PREFIX}-${i} >/dev/null 2>&1
    fi
done
if [ "${PLATFORM}" = "talos" ] && openstack server show ${LAB_NAME_PREFIX}-jump -f value -c status >/dev/null 2>&1; then
    _log INFO "  Deleting server ${LAB_NAME_PREFIX}-jump"
    openstack server delete ${LAB_NAME_PREFIX}-jump >/dev/null 2>&1
fi

# Wait for servers to terminate
_log INFO "Waiting for servers to terminate"
_wait_for_servers_term 180
_wait_for_servers_term 180

# Delete volumes if cinder enabled
if [ "${HYPERCONVERGED_CINDER_VOLUME:-false}" = "true" ]; then
    _log INFO "Deleting cinder volumes"
    for i in 0 1 2; do
        openstack volume delete --recursive ${LAB_NAME_PREFIX}-${i}-cv1 2>/dev/null || true
    done
    _wait_volumes_term 120
fi

# Detach router state before deleting lab ports so router-owned interfaces
# do not keep the network resources pinned.
_log INFO "Detaching router gateway and subnets"
openstack router set --no-gateway ${LAB_NAME_PREFIX}-router 2>/dev/null || true
openstack router remove subnet ${LAB_NAME_PREFIX}-router ${LAB_NAME_PREFIX}-compute-subnet 2>/dev/null || true
openstack router remove subnet ${LAB_NAME_PREFIX}-router ${LAB_NAME_PREFIX}-subnet 2>/dev/null || true

# Delete all ports (compute + metadata + mgmt + metalLB)
_log INFO "Deleting all ports"
_delete_all_ports "${LAB_NAME_PREFIX}"

# Delete security groups
_log INFO "Deleting security groups (rules first)"
_delete_security_groups "${LAB_NAME_PREFIX}"

# Delete subnets (need from router first)
_log INFO "Deleting subnets"
openstack subnet delete ${LAB_NAME_PREFIX}-compute-subnet 2>/dev/null || true
openstack subnet delete ${LAB_NAME_PREFIX}-subnet 2>/dev/null || true

# Delete networks
_log INFO "Deleting networks"
openstack network delete ${LAB_NAME_PREFIX}-compute-net 2>/dev/null || true
openstack network delete ${LAB_NAME_PREFIX}-net 2>/dev/null || true

# Delete router
_log INFO "Deleting router"
openstack router delete ${LAB_NAME_PREFIX}-router 2>/dev/null || true

# Delete keypair
_log INFO "Deleting keypair"
openstack keypair delete ${LAB_NAME_PREFIX}-key 2>/dev/null || true

# Clean up local SSH files
if [ -n "${HYPERCONVERGED_SSH_KEY_PATH:-}" ] || [ -n "${HYPERCONVERGED_SSH_PUB_KEY_PATH:-}" ]; then
    _log INFO "Skipping local SSH key cleanup because operator-provided HYPERCONVERGED_SSH_* paths are set"
elif [ -f "${SSH_KEY_PATH}" ] || [ -f "${SSH_PUB_KEY_PATH}" ]; then
    _log INFO "Cleaning local SSH keys"
    rm -f "${SSH_KEY_PATH}" "${SSH_PUB_KEY_PATH}"
fi

_log STEP "Uninstall complete"
