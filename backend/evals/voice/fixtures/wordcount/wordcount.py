"""Count words in text files."""

import argparse
import sys
from pathlib import Path


def count_words(text):
    return len(text.split())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Count the wrods in each file.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for path in args.files:
        print(f"{count_words(path.read_text())}\t{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
