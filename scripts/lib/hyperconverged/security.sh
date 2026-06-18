#!/usr/bin/env bash
# Security group functions for hyperconverged lab

# Each lib module resolves its own location to find helpers.sh
_SCRIPT_LOCAL="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
source "${_SCRIPT_LOCAL}/../helpers.sh"

function createCommonSecurityGroups() {
    if ! openstack security group show ${LAB_NAME_PREFIX}-http-secgroup -f value -c name >/dev/null 2>&1; then
        _log INFO "Creating HTTP security group"
        openstack security group create ${LAB_NAME_PREFIX}-http-secgroup >/dev/null 2>&1 || { _log ERROR "HTTP security group creation failed"; return 1; }
    else
        _log INFO "Reusing HTTP security group ${LAB_NAME_PREFIX}-http-secgroup"
    fi

    if ! openstack security group show ${LAB_NAME_PREFIX}-http-secgroup -f json 2>/dev/null | jq -r '.rules[].port_range_max' | grep -qx 443; then
        _log INFO "Creating HTTPS rule on ${LAB_NAME_PREFIX}-http-secgroup"
        openstack security group rule create ${LAB_NAME_PREFIX}-http-secgroup \
            --protocol tcp --ingress --remote-ip 0.0.0.0/0 --dst-port 443 --description "https" >/dev/null 2>&1 || { _log ERROR "HTTPS rule creation failed"; return 1; }
    else
        _log INFO "Reusing HTTPS rule on ${LAB_NAME_PREFIX}-http-secgroup"
    fi
    if ! openstack security group show ${LAB_NAME_PREFIX}-http-secgroup -f json 2>/dev/null | jq -r '.rules[].port_range_max' | grep -qx 80; then
        _log INFO "Creating HTTP rule on ${LAB_NAME_PREFIX}-http-secgroup"
        openstack security group rule create ${LAB_NAME_PREFIX}-http-secgroup \
            --protocol tcp --ingress --remote-ip 0.0.0.0/0 --dst-port 80 --description "http" >/dev/null 2>&1 || { _log ERROR "HTTP rule creation failed"; return 1; }
    else
        _log INFO "Reusing HTTP rule on ${LAB_NAME_PREFIX}-http-secgroup"
    fi

    if ! openstack security group show ${LAB_NAME_PREFIX}-secgroup -f value -c name >/dev/null 2>&1; then
        _log INFO "Creating internal security group"
        openstack security group create ${LAB_NAME_PREFIX}-secgroup >/dev/null 2>&1 || { _log ERROR "Internal security group creation failed"; return 1; }
    else
        _log INFO "Reusing internal security group ${LAB_NAME_PREFIX}-secgroup"
    fi

    if ! openstack security group show ${LAB_NAME_PREFIX}-secgroup -f json 2>/dev/null | jq -r '.rules[].description' | grep -qx "all internal traffic"; then
        _log INFO "Creating internal traffic rule on ${LAB_NAME_PREFIX}-secgroup"
        openstack security group rule create ${LAB_NAME_PREFIX}-secgroup \
            --protocol any --ingress --remote-ip 192.168.100.0/24 --description "all internal traffic" >/dev/null 2>&1 || { _log ERROR "Internal traffic rule creation failed"; return 1; }
    else
        _log INFO "Reusing internal traffic rule on ${LAB_NAME_PREFIX}-secgroup"
    fi
}
