"""命令行入口：python -m industry_fund --db fund.db [--host 127.0.0.1] [--port 8080]"""

from __future__ import annotations

import argparse

from .api import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="具身智能产业基金评审与里程碑拨款服务")
    parser.add_argument("--db", default="industry_fund.db", help="SQLite 数据库文件")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    print(f"服务启动: http://{args.host}:{args.port}  数据库: {args.db}")
    run_server(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
