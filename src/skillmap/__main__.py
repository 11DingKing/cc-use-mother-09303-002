"""启动入口：python -m skillmap [--host 127.0.0.1] [--port 8080] [--db skillmap.db]"""
from __future__ import annotations

import argparse
from http.server import HTTPServer

from .api import make_handler
from .app import SkillMapApp


def main() -> None:
    parser = argparse.ArgumentParser(description="职业技能标准映射后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="skillmap.db")
    args = parser.parse_args()

    app = SkillMapApp.open(args.db)
    server = HTTPServer((args.host, args.port), make_handler(app))
    print(f"技能标准映射后端已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
