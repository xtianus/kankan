# Version reporting

Status: implemented locally.

## 1. Checking an installation

From any directory, `kankan --version` prints `kankan <version>` and exits successfully. After pulling a checkout, read the single line in [kankan/VERSION](../kankan/VERSION) to compare it with the installation. Each commit changing the installed program carries a new version. Changes limited to tests or documentation outside the package can keep the same version.

## 2. Changes to the initial behaviour

[Initial functional specification §16](initial_spec_functional.md) gains a standalone CLI option that works before project setup.
