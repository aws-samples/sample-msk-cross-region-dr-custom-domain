#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# run_load.sh — continuous producer + consumer against the CUSTOM DOMAIN.
#
# Run this on the EC2 client (via SSM) in two panes, or with --mode to pick one.
# Both the producer and consumer connect to bootstrap.<domain>, so when DNS
# fails over to the DR cluster they re-bootstrap there automatically.
#
# Because MSK Replicator mirrors consumer-group offsets, the consumer resumes
# near where it left off after failover (at-least-once: expect a few duplicates,
# and a small gap = the asynchronous replication RPO).
#
# CROSS-REGION IAM NOTE (important): AWS_MSK_IAM authentication uses SigV4, which
# embeds the AWS Region. A flat custom domain like bootstrap.example.internal
# carries no region, so the msk-iam-auth library would fall back to the client's
# default region and FAIL against whichever cluster is in the other Region
# ("Access denied"). This is a documented IAM limitation (see the AWS blog FAQ:
# "the correct AWS Region must be included in the IAM authentication request").
#
# To keep the single-endpoint failover experience, we detect which Region's NLB
# bootstrap.<domain> currently resolves to and export AWS_REGION to match before
# connecting. On failover the next reconnect re-detects the active Region.
#
# Usage:
#   ./run_load.sh --mode producer   # emits one numbered message/sec
#   ./run_load.sh --mode consumer   # prints messages with a running count
#   ./run_load.sh --mode both       # producer in background + consumer (default)

set -uo pipefail

BOOTSTRAP="${BOOTSTRAP_DOMAIN:-bootstrap.example.internal:9098}"
CONFIG="${KAFKA_CLIENT_CONFIG:-/opt/kafka/client-iam.properties}"
TOPIC="${DEMO_TOPIC:-demo.heartbeat}"
GROUP="${DEMO_GROUP:-demo-consumer}"
PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
DR_REGION="${DR_REGION:-us-west-2}"
PRIMARY_MSK_CIDR_PREFIX="${PRIMARY_MSK_CIDR_PREFIX:-10.0.}"
MODE="both"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --topic) TOPIC="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

export PATH="$PATH:/opt/kafka/bin"

# Detect the active Region from what bootstrap.<domain> resolves to, and export
# AWS_REGION so the SigV4 IAM auth signs for the correct cluster. Primary MSK
# lives in PRIMARY_MSK_CIDR_PREFIX (e.g. 10.0.x), so a resolved IP in that range
# means we're on PRIMARY; anything else is the DR Region.
# Resolve which Region the bootstrap domain currently points to (no logging).
# Prints the region name. Primary MSK lives in PRIMARY_MSK_CIDR_PREFIX (e.g.
# 10.0.x); anything else is the DR Region.
detect_region() {
    local host="${BOOTSTRAP%%:*}" ip
    ip=$(dig +short "$host" 2>/dev/null | head -1)
    [[ -z "$ip" ]] && ip=$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1; exit}')
    if [[ "$ip" == ${PRIMARY_MSK_CIDR_PREFIX}* ]]; then echo "$PRIMARY_REGION"; else echo "$DR_REGION"; fi
}

set_active_region() {
    export AWS_REGION="$(detect_region)"
    echo "[region] bootstrap -> $(dig +short "${BOOTSTRAP%%:*}" 2>/dev/null | head -1) ; AWS_REGION=$AWS_REGION"
}
set_active_region

# Kill a process and its children (kafka-*.sh execs a java child).
kill_tree() {
    local pid="$1"
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
}

# Watchdog: while the Kafka client (PID $1) runs pinned to Region $2, poll the
# bootstrap domain; the instant it resolves to a DIFFERENT Region, kill the
# client tree so the restart loop re-detects and reconnects for the new Region.
# This makes failover auto-flip without a manual Ctrl-C. A real client needs
# equivalent supervision because the IAM/SigV4 Region is fixed per client
# instance and cannot change mid-process. $2 is captured BEFORE the client
# launches, so a freshly-restarted client isn't killed by a stale comparison.
watch_region_change() {
    local pid="$1" started_region="$2"
    while kill -0 "$pid" 2>/dev/null; do
        if [[ "$(detect_region)" != "$started_region" ]]; then
            echo "[watchdog] active Region changed from ${started_region} — reconnecting for new Region..."
            kill_tree "$pid"
            return
        fi
        sleep 3
    done
}

ensure_topic() {
    echo "Ensuring topic '$TOPIC' exists on $BOOTSTRAP ..."
    kafka-topics.sh --create --if-not-exists \
        --topic "$TOPIC" --partitions 3 --replication-factor 3 \
        --command-config "$CONFIG" \
        --bootstrap-server "$BOOTSTRAP" || true
}

# A failover changes both the active cluster AND the AWS Region the IAM auth must
# sign for, so a single long-running client would fail auth after the DNS flip.
# The producer/consumer therefore run in a restart loop: on every (re)start they
# re-detect the active Region (set_active_region) before reconnecting, so they
# follow the bootstrap domain across the failover. Each line is tagged with the
# active Region so the cutover is visible on screen (PRIMARY -> DR).
producer() {
    local n=0
    while true; do
        set_active_region
        local region_at_start="$AWS_REGION"
        echo "[producer] -> $TOPIC via $BOOTSTRAP  region=$AWS_REGION. Ctrl-C to stop."
        # Feed a numbered stream into the console-producer running in the
        # background so we hold its real PID. Fast delivery timeout also makes a
        # stalled produce fail quickly instead of blocking ~2 min.
        kafka-console-producer.sh \
            --topic "$TOPIC" \
            --producer.config "$CONFIG" \
            --bootstrap-server "$BOOTSTRAP" \
            --producer-property acks=all \
            --producer-property delivery.timeout.ms=10000 \
            --producer-property request.timeout.ms=8000 \
            --producer-property max.block.ms=10000 \
            < <(while :; do n=$((n+1)); echo "msg=${n} region=${region_at_start} ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; sleep 1; done) &
        local kpid=$!
        watch_region_change "$kpid" "$region_at_start"   # kills kpid when Region flips
        wait "$kpid" 2>/dev/null
        echo "[producer] disconnected — re-detecting region and reconnecting in 3s..."
        sleep 3
    done
}

consumer() {
    # Running count is persisted to a file so the tally survives a reconnect
    # (it keeps climbing on DR instead of resetting).
    local countfile="/tmp/.run_load_count"
    local total=0
    [[ -f "$countfile" ]] && total=$(cat "$countfile" 2>/dev/null || echo 0)
    while true; do
        set_active_region
        local region_at_start="$AWS_REGION"
        echo "[consumer] <- $TOPIC via $BOOTSTRAP  region=$AWS_REGION (group=$GROUP). Ctrl-C to stop."
        # Run the consumer in the background so we hold its REAL PID (piping to
        # awk directly would make $! the awk PID, and the watchdog could not kill
        # the consumer). Output streams live to the terminal, each line tagged
        # with the active Region, via a process-substitution tee into awk.
        kafka-console-consumer.sh \
            --topic "$TOPIC" \
            --group "$GROUP" \
            --consumer.config "$CONFIG" \
            --bootstrap-server "$BOOTSTRAP" \
            --property print.timestamp=true \
            > >(awk -v region="$region_at_start" -v start="$total" -v cf="$countfile" \
                '{ c=start+(++n); printf("[%s] recv#%d  %s\n", region, c, $0); fflush() }
                 END { print c > cf }') &
        local kpid=$!
        watch_region_change "$kpid" "$region_at_start"   # kills kpid when Region flips
        wait "$kpid" 2>/dev/null
        total=$(cat "$countfile" 2>/dev/null || echo "$total")
        echo "[consumer] disconnected — re-detecting region and reconnecting in 3s..."
        sleep 3
    done
}

ensure_topic
case "$MODE" in
    producer) producer ;;
    consumer) consumer ;;
    both)     producer & PROD_PID=$!; trap 'kill $PROD_PID 2>/dev/null' EXIT; sleep 2; consumer ;;
    *) echo "Unknown --mode: $MODE (use producer|consumer|both)" >&2; exit 1 ;;
esac
