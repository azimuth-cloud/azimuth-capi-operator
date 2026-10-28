# Contributing

We welcome contributions and suggestions for improvements to this code base.
Please check for relevant issues and PRs before opening a new one of your own.

## Making a contribution

### Running tests

The unit tests and linters are run using [tox](https://tox.wiki/) via
[uv](https://docs.astral.sh/uv/):

```
uv run tox
```

This runs the linters, the tests (with pytest) and a coverage report. To run a
single environment, or pass arguments through to pytest, use for example:

```
uv run tox -e 3.12 -- -k test_status
```

To fix lint issues automatically, run `uv run tox -e autofix`.

### Helm template snapshots

The CI in this repository uses the Helm
[unittest](https://github.com/helm-unittest/helm-unittest) plugin's
snapshotting functionality to check PRs for changes to the templated manifests.
Therefore, if your PR makes changes to the manifest templates or values, you
will need to update the saved snapshots to allow your changes to pass the
automated tests. The easiest way to do this is to run the helm unittest command
inside a docker container from the repo root.

```
docker run -i --rm -v $(pwd):/apps helmunittest/helm-unittest chart -u
```

where the `-u` option is used to update the existing snapshots.
