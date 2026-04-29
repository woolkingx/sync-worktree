# Build Artifact Spec

This document defines the `master/dist/` bundle as the deployable runtime artifact for `runner/`.
It describes what the bundle contains, what it must not contain, and the compatibility rules
the produced artifact must satisfy.

## Purpose

`master/` remains the source tree.

`master/dist/` is the build output root.

`runner/` is a deployment worktree that syncs only the runtime bundle produced under `master/dist/`.
The deployment target must not depend on the source tree layout.

## Bundle Layout

The canonical artifact is a bundle directory under `master/dist/`, for example:

```text
master/
  dist/
    sync-worktree/
      sync-worktree
      runtime/
      manifest.json
      LICENSE
```

The exact internal shape can evolve, but the bundle root must stay stable and self-contained.
The runtime may be a single executable or multiple files, as long as the bundle root is the
only deployable unit.

Minimum expectations:

- one executable entry point
- a runtime manifest or equivalent metadata when needed
- any files required for execution at runtime

## Runtime Assets

Runtime assets are files required for execution after deployment.

Typical runtime assets may include:

- the executable entry point
- runtime configuration or manifest files
- embedded data files required by the executable
- legal notices such as `LICENSE`

Runtime assets must be sufficient for `runner/` to operate without source-only directories.

## Exclusions

The following must not be bundled as runtime assets unless a specific artifact explicitly needs them:

- tests
- plans
- cleanup archives
- session logs
- development notes
- source-only documentation
- editor caches and Python bytecode

The bundle must not include working source tree scaffolding such as:

- `master/`
- `release/`
- `.cleanup/`
- `.sessions/`
- `__pycache__/`

## Compatibility Rules

The bundle root is the compatibility boundary.

- `runner/` consumes the bundle root from `master/dist/`, not the source tree.
- bundle contents may change internally, but the external runtime contract must remain stable.
- build output may be single-file or multi-file.
- sync rules should treat the bundle as an opaque deployable unit.

## Stability Requirements

The bundle must be reproducible from `master/`.

- repeated builds from the same source revision should produce an equivalent deployable shape
- runtime entry points must not rely on the caller's source tree
- deployment should not require access to development-only directories

If a file is required only for development or test execution, it does not belong in the runtime bundle.
