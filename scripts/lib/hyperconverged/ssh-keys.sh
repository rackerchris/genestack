#!/usr/bin/env bash
# SSH keypair management for hyperconverged lab

# Each lib module resolves its own location to find helpers.sh
_SCRIPT_LOCAL="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
source "${_SCRIPT_LOCAL}/../helpers.sh"

function configure_ssh_key_paths() {
    local prefix="${LAB_NAME_PREFIX:-genestack}"
    local default_key_path="${HOME}/.ssh/${prefix}-key.pem"
    local derived_pub_path

    export SSH_KEY_PATH="${SSH_KEY_PATH:-${HYPERCONVERGED_SSH_KEY_PATH:-${default_key_path}}}"

    if [[ "${SSH_KEY_PATH}" == *.pem ]]; then
        derived_pub_path="${SSH_KEY_PATH%.pem}.pub"
    else
        derived_pub_path="${SSH_KEY_PATH}.pub"
    fi

    export SSH_PUB_KEY_PATH="${SSH_PUB_KEY_PATH:-${HYPERCONVERGED_SSH_PUB_KEY_PATH:-${derived_pub_path}}}"
    export SSH_KEY_FILENAME="$(basename "${SSH_KEY_PATH}")"
    export SSH_PUB_KEY_FILENAME="$(basename "${SSH_PUB_KEY_PATH}")"
}

function createOrUpdateKeypair() {
    local prefix="${LAB_NAME_PREFIX:-genestack}"
    local local_pub
    local remote_pub
    configure_ssh_key_paths

    if [ ! -d "$(dirname "${SSH_KEY_PATH}")" ]; then
        _log INFO "Creating SSH directory"
        mkdir -p "$(dirname "${SSH_KEY_PATH}")"
        chmod 700 "$(dirname "${SSH_KEY_PATH}")"
    else
        _log INFO "Reusing SSH directory"
    fi

    if ! openstack keypair show ${prefix}-key -f value -c name >/dev/null 2>&1; then
        if [ ! -f "${SSH_KEY_PATH}" ]; then
            _log INFO "Generating new SSH keypair"
            openstack keypair delete ${prefix}-key >/dev/null 2>&1 || true
            openstack keypair create ${prefix}-key > "${SSH_KEY_PATH}" 2>/dev/null
            chmod 600 "${SSH_KEY_PATH}"
            openstack keypair show ${prefix}-key --public-key > "${SSH_PUB_KEY_PATH}" 2>/dev/null
        else
            if [ ! -f "${SSH_PUB_KEY_PATH}" ]; then
                _log INFO "Creating public key from existing local private key"
                ssh-keygen -y -f "${SSH_KEY_PATH}" > "${SSH_PUB_KEY_PATH}"
            fi
            _log INFO "Creating OpenStack keypair from existing local key"
            openstack keypair create ${prefix}-key --public-key "${SSH_PUB_KEY_PATH}" >/dev/null 2>&1
        fi
    else
        if [ ! -f "${SSH_PUB_KEY_PATH}" ] && [ -f "${SSH_KEY_PATH}" ]; then
            _log INFO "Creating public key from existing local private key"
            ssh-keygen -y -f "${SSH_KEY_PATH}" > "${SSH_PUB_KEY_PATH}"
        fi

        if [ -f "${SSH_PUB_KEY_PATH}" ]; then
            local_pub=$(tr -d '\n' < "${SSH_PUB_KEY_PATH}")
            remote_pub=$(openstack keypair show ${prefix}-key -f value -c public_key 2>/dev/null || true)
            if [ -z "${remote_pub}" ] || [ "${local_pub}" != "${remote_pub}" ]; then
                _log INFO "Reconciling OpenStack keypair ${prefix}-key to match local key"
                openstack keypair delete ${prefix}-key >/dev/null 2>&1 || true
                openstack keypair create ${prefix}-key --public-key "${SSH_PUB_KEY_PATH}" >/dev/null 2>&1
            else
                _log INFO "Reusing OpenStack keypair ${prefix}-key"
            fi
        else
            _log INFO "Reusing OpenStack keypair ${prefix}-key"
        fi
    fi

    ssh-add "${SSH_KEY_PATH}" 2>/dev/null || true
}
