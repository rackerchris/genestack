#!/bin/bash
# R3-P2 lifecycle suite with post-op throughput assertions
OUT=/private/var/folders/z3/zw0bs54535x5bm_b_9jspkn80000gn/T/opencode/qos-results-r3
KEY=$HOME/.ssh/dfw-dev-me.pem
SSH() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ControlPath=none -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "ubuntu@$1" "${@:2}" 2>/dev/null; }
source /Users/chris.breu/code/venv/bin/activate
ME="openstack --os-cloud dfw-dev-me"; ADMIN="openstack --os-cloud dfw-dev-admin"
A=7701710a-98aa-471d-af54-09041ca3802b; PA=82f634b4-ae39-4d56-9842-1b44d75ad2a8; FA=148.62.108.199
B=2a31a9fd-70c2-4599-b3d8-2d8021fad287; PB=e0fe1960-59f8-4906-8480-f881752a2145; FB=148.62.108.188
C=f0762cd3-e654-4153-8de4-d1678b1dfcda; PC=751ef2cd-d48a-47f9-86fa-6c131984a721; FC=148.62.108.181
D=2182745c-4347-4b92-8380-4d5ec5de0499; PD=aa883cab-ece1-4f13-af39-ebc130ef91a6; FD=148.62.108.197
E=5f00a585-aa78-4c48-97b2-0420bf82148e; PE=e825e853-3172-47c0-abdf-4a72c788e95e; FE=148.62.108.239
LITE=13157731-a144-4b55-a9b9-40ae46509476; PLUS=1206258d-40e8-4f4f-8aef-738ea4771119; ELITE=0b2e90b4-dfe4-4ac7-9fba-c0c713690064
bps() { python3 -c "
import json
try:
    d=json.load(open('$1')); print(round(d['end']['sum_received']['bits_per_second']/1e9,2))
except Exception: print('ERR')"; }
spot() { # $1 senderFip $2 receiverPriv $3 parallel $4 tag  (60s)
  local f=$OUT/spot_$4.json
  SSH $1 "iperf3 -c $2 -t 60 -P $3 -J" > $f
  echo "   spot $4: $(bps $f) Gbit/s"
}
ens() { for f in $FA $FB $FC $FD $FE; do SSH $f 'pgrep -x iperf3 >/dev/null || iperf3 -s -D'; done; }

echo "=== L1 revert on B (lite→plus→revert→lite→plus→lite→revert→lite? no: up/revert, up/revert) ==="
$ME server resize --flavor gp.8.4.8 --wait $B
echo "  at VERIFY_RESIZE: qos=$($ADMIN port show $PB -c qos_policy_id -f value) (want $PLUS)"
$ME server resize revert $B; sleep 15
echo "  after revert: status=$($ME server show $B -c status -f value) qos=$($ADMIN port show $PB -c qos_policy_id -f value) (want $LITE)"
$ME server resize --flavor gp.8.4.8 --wait $B
echo "  hop2 VERIFY_RESIZE: qos=$($ADMIN port show $PB -c qos_policy_id -f value) (want $PLUS)"
$ME server resize revert $B; sleep 15
echo "  revert2: status=$($ME server show $B -c status -f value) qos=$($ADMIN port show $PB -c qos_policy_id -f value) (want $LITE)"
$ADMIN server migration list --server $B -f json | python3 -c "import json,sys; [print('  mig',m['Id'],m['Status'],m['UUID'][:8]) for m in json.load(sys.stdin)]"
ens; spot $FB 192.168.100.35 1 B-after-revert

echo "=== L2 resize+confirm + DOWN grade on A (lite→plus confirm → down→lite confirm) ==="
$ME server resize --flavor gp.8.4.8 --wait $A
echo "  VR: qos=$($ADMIN port show $PA -c qos_policy_id -f value) (want $PLUS)"
$ME server resize confirm $A; sleep 8
echo "  after confirm: flavor=$($ME server show $A -f json | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"flavor\"][\"id\"])') qos=$($ADMIN port show $PA -c qos_policy_id -f value)"
ens; spot $FA 192.168.100.25 4 A-as-plus
$ME server resize --flavor gp.8.2.4 --wait $A
echo "  down-VR: qos=$($ADMIN port show $PA -c qos_policy_id -f value) (want $LITE)"
$ME server resize confirm $A; sleep 8
echo "  after downgrade: qos=$($ADMIN port show $PA -c qos_policy_id -f value) (want $LITE)"
ens; spot $FA 192.168.100.25 1 A-back-to-lite

echo "=== L3 cold migration on C (-admin) ==="
$ADMIN server migrate --wait $C; $ME server resize confirm $C 2>/dev/null; sleep 5
echo "  host=$($ADMIN server show $C -c OS-EXT-SRV-ATTR:host -f value) qos=$($ADMIN port show $PC -c qos_policy_id -f value) (want $PLUS) status=$($ME server show $C -c status -f value)"
ens; spot $FC 192.168.100.35 4 C-after-coldmig

echo "=== L4 live migration on D (-admin) ==="
$ADMIN server migrate --live-migration --wait $D; sleep 5
echo "  host=$($ADMIN server show $D -c OS-EXT-SRV-ATTR:host -f value) qos=$($ADMIN port show $PD -c qos_policy_id -f value) (want $PLUS) status=$($ME server show $D -c status -f value)"
ens; spot $FD 192.168.103.124 4 D-after-livemig

echo "=== L5 tier ladder on E: elite→gp.8.8.16(plus)→gp.8.4.8(plus)→gp.8.2.4(lite)→gp.8.16.32(elite) ==="
$ME server resize --flavor gp.8.8.16 --wait $E
echo "  VR1: qos=$($ADMIN port show $PE -c qos_policy_id -f value) (want $PLUS)"
$ME server resize confirm $E; sleep 5
$ME server resize --flavor gp.8.4.8 --wait $E
echo "  VR2: qos=$($ADMIN port show $PE -c qos_policy_id -f value) (want $PLUS)"
$ME server resize confirm $E; sleep 5
$ME server resize --flavor gp.8.2.4 --wait $E
echo "  VR3: qos=$($ADMIN port show $PE -c qos_policy_id -f value) (want $LITE)"
$ME server resize confirm $E; sleep 5
ens; spot $FE 192.168.100.25 1 E-as-lite
$ME server resize --flavor gp.8.16.32 --wait $E
echo "  VR4: qos=$($ADMIN port show $PE -c qos_policy_id -f value) (want $ELITE)"
$ME server resize confirm $E; sleep 8
echo "  E final: flavor=$($ME server show $E -f json | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"flavor\"][\"id\"])') qos=$($ADMIN port show $PE -c qos_policy_id -f value) host=$($ADMIN server show $E -c OS-EXT-SRV-ATTR:host -f value)"
ens; spot $FE 192.168.103.124 8 E-back-to-elite
echo "LIFECYCLE DONE"
