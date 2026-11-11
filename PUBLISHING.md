# Publishing

Nothing here is published yet.

1. Check everything, against real boxd too:

   ```sh
   uv sync
   uv run ruff check . && uv run ruff format --check .
   uv run mypy src tests examples
   uv run pytest
   BOXD_API_KEY=... uv run pytest -m live
   ```

   The live tests run against production unless `BOXD_BASE_URL` is set. They
   include Inspect's portable sandbox checks on a machine and in three compose
   setups; the root-container setup marks three checks `xfail(strict=True)`,
   the same three Inspect's own Docker sandbox expects root to fail.

2. Run the four examples with a real model (`ANTHROPIC_API_KEY` set), as listed
   in the README, and each README snippet.

3. Build and check the package:

   ```sh
   rm -rf dist && uv build
   uvx twine check dist/*
   unzip -l dist/*.whl
   ```

   The wheel holds `boxd_inspect/` with `py.typed`, and the entry point
   `inspect_ai: boxd_inspect = boxd_inspect._registry`.

4. Install the wheel in a clean virtualenv and run the README quick start from
   it, so you test what users get. `inspect sandbox cleanup boxd` should list
   no machines afterwards.

5. Set the date in `CHANGELOG.md`. The first release ships `0.1.0` as it stands
   in `src/boxd_inspect/_version.py`; bump it there for every later release.

6. Check that the README banner uses its full `raw.githubusercontent.com` URL:
   PyPI doesn't show relative images.

7. Publish, then tag:

   ```sh
   uv publish
   git tag v0.1.0 && git push --tags
   ```

8. Open the pull request that adds `boxd-inspect` to the extensions listing in
   Inspect's docs (`docs/extensions/extensions.yml` in `UKGovernmentBEIS/inspect_ai`).
