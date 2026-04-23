import argparse

from . import initialize
from .runner import run_queue


def main() -> None:
    parser = argparse.ArgumentParser(prog="repobot")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="Clone the configured repo into workspace/")

    fetch_p = sub.add_parser("fetch", help="Fetch + triage items for a queue")
    fetch_p.add_argument("queue_id")

    serve_p = sub.add_parser("serve", help="Run the web UI")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    cmd = args.cmd or "init"

    if cmd == "init":
        path = initialize()
        print(f"[repobot] ready at {path}")
    elif cmd == "fetch":
        result = run_queue(args.queue_id, wait_for_triage=True)
        n = len(result.get("items", []))
        print(f"[repobot] queue '{args.queue_id}' now has {n} item(s)")
    elif cmd == "serve":
        import uvicorn
        uvicorn.run("repobot.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
