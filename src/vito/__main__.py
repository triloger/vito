"""Allow ``python -m vito``."""

from vito.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
