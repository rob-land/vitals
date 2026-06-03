"""Vitals — entry point."""

import sys


def main(argv=None):
    # Configure logging before anything else so import-time messages land
    # in the file. configure_logging() reads VITALS_DEBUG / `--debug`.
    from vitals.logging_setup import configure_logging
    configure_logging()

    from vitals.application import VitalsApplication
    return VitalsApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
