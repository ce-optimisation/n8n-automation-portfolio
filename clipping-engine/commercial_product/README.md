# Customer workspace preflight

This utility checks a Windows local-first installation's proposed workspace layout without creating directories, reading credential values or starting Clipping Engine. It uses Python 3.10 or later and only the standard library.

Create a dedicated directory for each customer. Each directory needs an existing `data` directory and a separate `config/.env` file. Store the descriptor away from any customer media tree if convenient. It holds paths and workspace identifiers only.

```json
{
  "version": 1,
  "workspaces": [
    {"id": "customer-one", "root": "C:/ClippingWorkspaces/customer-one", "port": 8766},
    {"id": "customer-two", "root": "C:/ClippingWorkspaces/customer-two", "port": 8767}
  ]
}
```

From the project root, run `python -m commercial_product.workspace_preflight workspaces.json`. Exit code 0 prints the non-secret environment overrides for each instance. Exit code 1 reports invalid configuration. Validation requires unique identifiers, non-overlapping canonical roots, distinct loopback ports, paths contained under each workspace root and physically distinct configuration files. The output fixes the host at `127.0.0.1`.

A future launcher must apply these overrides to a clean child environment and use the staged engine that supports `CLIPENGINE_WORKSPACE_ID`. The current preflight does not reserve ports, start processes, audit media-tree links, or enforce operating-system access control. Workspaces running under one OS account still share that account's permissions.

## Verification

Ran `python -m unittest -v commercial_product.test_workspace_preflight` on Windows against synthetic temporary roots. Five tests passed, covering two separate configurations, distinct namespace and port output, overlapping roots, shared configuration hard links, invalid settings, and an absent configuration. No existing customer or production data was modified.
