# Myrmex Campaign Supervisor (`myrmex-head`)

## Overview

`myrmex-head` is the persistent autonomous campaign supervisor for Myrmex. It executes the closed-loop autonomous cycle across work units without requiring an open terminal or interactive user session.

## Architecture

* **Foreground Execution**: Designed to run cleanly in foreground under `systemd --user`. No homebrewed daemons or fragile PID files.
* **Exclusive Leasing**: Acquires an exclusive lease per active campaign, with automatic renewal via heartbeat. If a supervisor crashes, its lease expires cleanly and can be reclaimed by the next instance.
* **Reconciliation-First Principle**: Always runs `reconcile` before attempting any mutation or dispatch. Interrupted runs, deadlocks, and stale states are resolved idempotently without duplicating external effects.
* **Closed-Loop WU Lifecycle**:
  ```text
  ready
  → collecting-context
  → implementing
  → verifying
  → remediating (budget-governed)
  → running-ci
  → committing
  → delivering
  → completed
  ```
* **Graceful Termination**: Handles `SIGTERM` and `SIGINT` to safely persist running progress and release held leases immediately.

## Quick Start

### 1. Run in Once Mode (Testing / Automation)

```bash
# Run a single reconciliation and dispatch cycle
myrmex-head --once
```

### 2. Run Under systemd --user

```bash
# Install and enable service
./scripts/install-service.sh

# Check status
systemctl --user status myrmex-head.service

# View live journal logs
journalctl --user -u myrmex-head.service -f
```

### 3. Enable Linger for Host Reboots

By default, `systemd --user` services only run while a user session is active. To enable the supervisor to run in background across host restarts without an active login session:

```bash
loginctl enable-linger $USER
```

To verify linger status:
```bash
loginctl show-user $USER --property=Linger
```

### 4. Stopping and Uninstalling

```bash
# Stop service
systemctl --user stop myrmex-head.service

# Uninstall service
./scripts/uninstall-service.sh
```

## CLI Reference

```text
usage: myrmex-head [-h] [--once] [--campaign-id CAMPAIGN_ID]
                   [--interval INTERVAL] [--state-home STATE_HOME]

options:
  -h, --help            show this help message and exit
  --once                Run a single reconciliation/dispatch cycle and exit
  --campaign-id CID     Target specific campaign ID
  --interval SECONDS    Polling interval in seconds (default: 5)
  --state-home DIR      Custom XDG_STATE_HOME path
```
