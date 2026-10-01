import argparse

from .server import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="技能授权与排班管理")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    args = parser.parse_args()
    serve(args.host, args.port)
