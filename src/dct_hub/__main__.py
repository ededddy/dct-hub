"""`python -m dct_hub --config charts-tool.yml` or the `dct-hub` script."""

import argparse

import uvicorn

from .api import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="dct-hub")
    parser.add_argument("--config", default="charts-tool.yml", help="Path to charts-tool.yml")
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
