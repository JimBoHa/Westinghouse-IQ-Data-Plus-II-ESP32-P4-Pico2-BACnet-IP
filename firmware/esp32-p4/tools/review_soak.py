#!/usr/bin/env python3
"""Re-evaluate a saved health soak without contacting hardware."""
import argparse
import json
from pathlib import Path
import sys
from soak_monitor import load_records, review_records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log",type=Path)
    parser.add_argument("--minimum-duration",type=float,default=86400,
                        help="Required measured duration; default 24 hours")
    args = parser.parse_args()
    result = review_records(load_records(args.log),args.minimum_duration)
    print(json.dumps(result,indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError,ValueError,KeyError,TypeError) as error:
        sys.exit(str(error))
