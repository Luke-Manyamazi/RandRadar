"""RandRadar - ZAR arbitrage study logger.

    python run.py --check            # fetch every market once, confirm endpoints work
    python run.py                    # start logging (Ctrl+C to stop)
    python run.py --config my.yaml
"""
import argparse
import asyncio
import logging
import sys

from randradar import config as config_mod
from randradar import runner


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--check", action="store_true", help="test every endpoint once and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = config_mod.load(args.config)

    if args.check:
        return 0 if asyncio.run(runner.check(cfg)) else 1
    asyncio.run(runner.main(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
