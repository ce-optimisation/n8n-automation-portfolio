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

## Opt-in foreground launcher

After preflight, a controlled local developer test can start one prepared workspace:

```text
python -m commercial_product.workspace_launcher workspaces.json customer-one --release-root C:/TestRelease --python C:/TestPython/python.exe
```

Run the command in a separate terminal for each workspace. If required, point `--tools-dir` at the verified local FFmpeg binaries. Use a trusted test release with `CLIPENGINE_WORKSPACE_ID` support. Supply its dependencies separately. The launcher passes the workspace overrides and a small allowlist of system variables, with an explicit tool path. Ordinary inherited API keys, provider secrets and Python import overrides are omitted. It checks that the intended loopback port is free and waits for the worker health endpoint there. Ctrl+C stops the immediate launched process. The synthetic fixture has no descendants.

The launcher has been exercised with a synthetic engine only. A port check cannot reserve the address across process startup. The launcher does not manage Windows services, independently prove the responding worker's identity, control an entire Windows process tree, or enforce a security boundary between workspaces under the same OS account. Before customer release, verify real dependency packaging, process-tree shutdown, bind failures, workspace access controls and the full provider-free installed workflow.

## Verification

Ran `python -m unittest -v commercial_product.test_workspace_preflight` on Windows against synthetic temporary roots. Five tests passed, covering two separate configurations, distinct namespace and port output, overlapping roots, shared configuration hard links, invalid settings, and an absent configuration. No existing customer or production data was modified.

The combined preflight and launcher run passed seven tests. Two synthetic workers ran concurrently. Each responded on its own loopback port with its own process ID, config marker and data marker. The launcher rejected an occupied port before starting a process, omitted synthetic inherited provider secrets and stopped both fixture processes. No real engine, provider connection or installer was used.

## Release manifest verification

`python -m commercial_product.release_manifest RELEASE_DIRECTORY MANIFEST_FILE --manifest-sha256 TRUSTED_SHA256`

Give the verifier an already unpacked directory and a version 1 JSON manifest stored outside that directory. The manifest format is `{"version":1,"files":[{"path":"clipengine/version.py","sha256":"<64 lowercase hexadecimal characters>"}]}`. Get the SHA-256 of the manifest itself through a separately authenticated release channel. A hash published alongside an untrusted bundle does not establish authenticity.

The verifier checks that the manifest digest matches, that every ordinary file under the release root is listed exactly once, and that all file digests match. It rejects traversal, absolute and Windows alternate-stream paths, case aliases, hard links, symbolic links and Windows reparse points. Keep the checked tree under trusted write control throughout verification and any later use. The verifier does not install, unpack, sign, publish or bind the release to the launcher. Signed distribution, safe extraction, package provenance and a real update/rollback test are separate release gates.

Five synthetic Windows manifest tests passed, covering a valid exact bundle, file and manifest tampering, missing and undeclared files, path escape and alias inputs, and an external hard link.

## Local release staging and rollback

Use a dedicated existing store directory separate from workspaces and source bundles:

```text
python -m commercial_product.release_store STORE stage SOURCE_BUNDLE MANIFEST --manifest-sha256 TRUSTED_SHA256
python -m commercial_product.release_store STORE activate TRUSTED_SHA256
python -m commercial_product.workspace_launcher workspaces.json customer-one --release-store STORE --python C:/TestPython/python.exe
python -m commercial_product.release_store STORE rollback
```

Staging verifies the source bundle against the trusted manifest digest, copies it into temporary storage, verifies the copy and places it under the digest-derived release ID. Activation and rollback reverify their target and update the current and previous pointers atomically under a local selection lock. They affect future launches only. Stop the affected workspace worker before updating it and verify schema compatibility or take a workspace database backup before launch. This operation rolls back the selected code bundle, without restoring or writing workspace data. The store and trusted manifest publisher remain part of the local trust boundary.

Selecting a store through the launcher reverifies the current bundle before launch and disables Python bytecode writes into the release. The Windows synthetic suite passed 16 tests, including stage, activation, update, rollback, invalid bundle rejection and launching from a selected bundle that continued to pass verification after the worker started. A Windows service, installer signing, package extraction and real database migration recovery were outside this check.

## Runtime and licence evidence

`python -m commercial_product.runtime_inventory --search-path PATH_TO_SITE_PACKAGES --binary PATH_TO_FFMPEG --binary PATH_TO_FFPROBE`

The inventory reads distribution metadata without importing target packages. It records versions, declared licence expressions, legacy licence indicators, declared licence-file names and metadata text hashes. Optional binary paths are hashed without executing them. The report leaves binary licence evidence unassessed. Licence fields report what the specific distribution declares. A metadata hash does not establish wheel identity or redistribution rights. [Python packaging metadata specification](https://packaging.python.org/en/latest/specifications/core-metadata/).

`runtime-evidence-20261126.json` records a read-only snapshot of the existing Windows Python environment and the two FFmpeg tools located on PATH. It contains 62 distributions under CPython 3.10.4, with 36 licence expressions and 26 legacy indicators. This is evidence about the inspected machine. Build a fresh inventory for the exact customer artifacts, including wheel and binary hashes, licence texts, notices, source obligations, fonts and models. A signed release, authoritative product bill of materials and redistribution clearance remain open.

Three synthetic inventory tests passed. The combined commercial-product suite passed 19 tests on Windows. No binary was executed to generate the inventory, and no production files were modified.
