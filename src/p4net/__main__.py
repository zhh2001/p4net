"""Allow `python -m p4net <args>` to run the same entry point as the console script."""

from p4net.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
