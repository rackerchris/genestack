#!/bin/bash
# Round-3 3-tier QoS enforcement matrix
OUT=/private/var/folders/z3/zw0bs54535x5bm_b_9jspkn80000gn/T/opencode/qos-results-r3
mkdir -p $OUT
KEY=$HOME/.ssh/dfw-dev-me.pem
SSH() { ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ControlPath=none -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "ubuntu@$1" "${@:2}" 2>/dev/null; }
priv() { case $1 in A) echo 192.168.100.35;; B) echo 192.168.100.25;; C) echo 192.168.103.124;; D) echo 192.168.102.199;; E) echo 192.168.102.141;; esac; }
fip()  { case $1 in A) echo 148.62.108.199;; B) echo 148.62.108.188;; C) echo 148.62.108.181;; D) echo 148.62.108.197;; E) echo 148.62.108.239;; esac; }
ensure_servers() { for h in A B C D E; do SSH $(fip $h) 'pgrep -x iperf3 >/dev/null || iperf3 -s -D'; done; }
bps() { python3 -c "
import json
try:
    d=json.load(open('$1')); print(round(d['end']['sum_received']['bits_per_second']/1e9,2))
except Exception: print('ERR')"; }
run_pair() {
  local tag=$1 sender=$2 receiver=$3 parallel=$4
  local sf=$(fip $sender) rp=$(priv $receiver)
  local f=$OUT/${tag}_${sender}-to-${receiver}_P${parallel}.json
  SSH $sf "iperf3 -c $rp -t 60 -P $parallel -J" > $f
  echo "$(date +%H:%M:%S) $tag $sender->$receiver P$parallel = $(bps $f) Gbit/s"
}

ensure_servers
echo "=== R3 matrix start $(date) ==="
# lite tier
run_pair r3 B A 1
run_pair r3 B A 4
# plus tier
run_pair r3 C B 1
run_pair r3 C B 4
run_pair r3 D A 1
# elite tier (new coverage)
run_pair r3 E A 1
run_pair r3 E A 4
# elite fan-out: sum must cap at 4G
run_pair r3fan E A 1 &
run_pair r3fan E B 1 &
run_pair r3fan E C 1 &
wait
# 5-VM mesh: each sender must equal its own tier limit
run_pair r3mesh B A 1 &
run_pair r3mesh A B 1 &
run_pair r3mesh C D 1 &
run_pair r3mesh D C 1 &
run_pair r3mesh E A 1 &
wait
ensure_servers
echo "=== R3 matrix done $(date) ==="
