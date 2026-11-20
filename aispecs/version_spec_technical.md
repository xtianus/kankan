# Version reporting implementation

Status: implemented locally.

## 1. Package version

Setuptools reads the distribution version from [kankan/VERSION](../kankan/VERSION) and includes the file in the distribution. The Python package reads the same resource for `__version__`. Maintainers bump the file in the same commit as an installable change.

## 2. Changes to the initial behaviour

The top-level parser in [initial technical specification §16](initial_spec_technical.md) handles `--version` through argparse before command dispatch.
