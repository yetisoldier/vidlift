"""Frozen Windows command-line entry point."""


def main():
    from vidlift.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
