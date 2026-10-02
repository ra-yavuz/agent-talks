"""Human controls, deliberately not exposed as agent tools."""
import argparse
import json

from broker import Broker, CLOSE_QUESTION


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Provided as is, without warranty. You accept all risk from installation and use.')
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List channels without revealing member tokens")
    close = sub.add_parser("close", help="Stop a channel and release its tool guard; preserve history")
    close.add_argument("channel")
    reopen = sub.add_parser("reopen", help="Reopen a closed channel with the same members and history; orchestrator goes next")
    reopen.add_argument("channel")
    recover = sub.add_parser("recover", help="Replace one or both sessions, preserving channel history")
    recover.add_argument("channel")
    recover.add_argument("--orchestrator", metavar="SESSION_ID")
    recover.add_argument("--worker", metavar="SESSION_ID")
    args = parser.parse_args()
    broker = Broker()
    try:
        if args.command == "list":
            print(json.dumps([dict(row) for row in broker.db.execute("SELECT * FROM channels")], indent=2))
        elif args.command == "reopen":
            print(json.dumps(broker.reopen(args.channel), indent=2))
        elif args.command == "recover":
            replacements = {role: getattr(args, role) for role in ("orchestrator", "worker") if getattr(args, role)}
            plan = broker.recovery_plan(args.channel, replacements)
            print(json.dumps(plan, indent=2))
            print("History will be preserved. Replaced sessions lose channel access; stop their running jobs. "
                  "Open channels return to the orchestrator. Closed channels stay closed.")
            try:
                answer = input("Replace these sessions? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Not confirmed. Membership is unchanged.")
                return
            print(json.dumps(broker.recover(plan, confirmed=True), indent=2))
            print("Call register in each replacement session using the same channel and role.")
        else:
            current = broker.db.execute("SELECT * FROM channels WHERE name=?", (args.channel,)).fetchone()
            if not current:
                parser.error("Unknown channel")
            if current["state"] == "closed":
                print(f"{args.channel} is already closed.")
                return
            try:
                answer = input(f"Channel: {args.channel}\n{CLOSE_QUESTION} [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("Not confirmed. The channel remains open.")
                return
            broker.human_close(args.channel, current["revision"])
            print(f"Closed {args.channel}. Waiting calls will return; history is preserved.")
    finally:
        broker.db.close()


if __name__ == "__main__":
    main()
