from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import get_settings
from .observability import setup

log = logging.getLogger("sn_agent")


def main(argv: list[str] | None = None) -> int:
    # A Windows terminal or pipe in cp1252 cannot print every character a note
    # may hold; show a "?" instead of crashing after the work is done.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="sn-agent", description="ServiceNow ticket resolution agent and help desk assistant"
    )
    # Accepted before or after the command: `sn-agent -v resolve X` or `sn-agent resolve X -v`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true",
        # SUPPRESS: otherwise the subcommand's default False overwrites a -v
        # given before the command.
        default=argparse.SUPPRESS,
        help="show every search, every TCS GenAI Lab call with its timing and cost, and every HTTP request",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve", help="research one ticket", parents=[common])
    resolve.add_argument("number")
    resolve.add_argument("--table", default="incident")
    resolve.add_argument("--dry-run", action="store_true", help="print the note instead of posting it")
    resolve.add_argument("--force", action="store_true", help="run even if this ticket was answered already")
    resolve.add_argument("--json", action="store_true", help="print the full run result as JSON")

    sweep = sub.add_parser(
        "sweep", help="work every open ticket that still needs the agent, most urgent first", parents=[common]
    )
    sweep.add_argument("--list", action="store_true", help="show the queue and stop")
    sweep.add_argument("--dry-run", action="store_true", help="research each ticket but post nothing")
    sweep.add_argument("--limit", type=int, default=None, help="only the first N in the queue")
    sweep.add_argument("--workers", type=int, default=None, help="tickets to work on at once")

    serve = sub.add_parser(
        "serve",
        help="run continuously: workers and the sweep (plus the webhook, if WEBHOOK_ENABLED=true)",
        parents=[common],
    )
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)

    intake = sub.add_parser(
        "intake", help="chat with the help desk assistant in this terminal (text only)", parents=[common]
    )
    intake.add_argument("--email", default="", help="who you are, for the caller on any ticket raised")

    voice = sub.add_parser(
        "voice",
        help="run the voice agent worker for LiveKit: voice dev | voice start | voice download-files",
    )
    voice.add_argument("livekit_args", nargs=argparse.REMAINDER)

    web = sub.add_parser("web", help="run the chatbot page and the ops console", parents=[common])
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)

    users = sub.add_parser("users", help="manage logins for the web app", parents=[common])
    users_sub = users.add_subparsers(dest="action", required=True)
    add = users_sub.add_parser("add", help="add a login, or reset its password")
    add.add_argument("email")
    add.add_argument("--name", default="")
    add.add_argument("--role", choices=["user", "ops"], default="user")
    users_sub.add_parser("list", help="list logins")
    remove = users_sub.add_parser("remove", help="remove a login")
    remove.add_argument("email")

    index = sub.add_parser("index", help="the local copy of resolved tickets used by KNOWLEDGE_SOURCE=index", parents=[common])
    index_sub = index.add_subparsers(dest="action", required=True)
    sync = index_sub.add_parser("sync", help="copy resolved tickets in; after the first run, only what changed")
    sync.add_argument("--full", action="store_true", help="re-read everything, not just what changed")
    sync.add_argument("--days", type=int, default=None, help="how far back a full read goes (default: SEARCH_RECENCY_DAYS)")
    sync.add_argument("--watch", type=float, default=0, metavar="MINUTES", help="keep syncing every MINUTES")
    index_sub.add_parser("status", help="how many tickets are indexed, and when it last synced")
    find = index_sub.add_parser("search", help="try a search against the index")
    find.add_argument("text")
    find.add_argument("--limit", type=int, default=8)

    args = parser.parse_args(argv)
    if args.command == "voice":
        # LiveKit's CLI sets up its own logging and worker processes.
        from .intake.voice import run as run_voice

        return run_voice(args.livekit_args or ["dev"])

    settings = get_settings()
    setup(settings, verbose=args.verbose)

    if args.command == "intake":
        return _intake(args, settings)
    if args.command == "web":
        from .web.app import run as run_web

        print(f"Chatbot and ops console on http://{args.host}:{args.port}  (Ctrl+C to stop)")
        run_web(args.host, args.port)
        return 0
    if args.command == "users":
        return _users(args, settings)
    if args.command == "index":
        return _index(args, settings)

    if args.command == "resolve":
        from .factory import build_service

        service = build_service(settings, dry_run=args.dry_run)
        try:
            outcome = service.resolve(args.number, args.table, force=args.force)
        except Exception as exc:
            if args.verbose:
                log.exception("could not process %s", args.number)
            else:
                log.error("could not process %s: %s", args.number, exc)
                log.error("run again with -v for the full error")
            return 1
        if outcome.skipped_reason:
            print(f"skipped: {outcome.skipped_reason}")
            return 0
        if args.json:
            print(json.dumps(outcome.result.model_dump(mode="json"), indent=2))
        else:
            print(outcome.published.body)
            print(f"\n[{'posted' if outcome.published.delivered else 'not posted (dry-run)'}]")
        return 0

    if args.command == "sweep":
        return _sweep(args, settings)

    if args.command == "serve":
        if settings.webhook_enabled:
            import uvicorn

            print(f"Webhook listening on {args.host}:{args.port}, sweep every {settings.sweep_interval_minutes:g} min")
            uvicorn.run("sn_agent.api.app:app", host=args.host, port=args.port)
            return 0
        return _run_without_webhook(settings)

    return 1


def _intake(args, settings) -> int:
    """The text channel in a terminal: the quickest way to try the assistant
    against the live instance. Tickets are only drafted unless
    INTAKE_CREATE_TICKETS=true."""

    import asyncio

    from .intake.chat import TextChat, chat_client, chat_model
    from .intake.session import IntakeSession, build_services
    from .recorder import open_store

    services = build_services(settings, store=open_store(settings))
    session = IntakeSession(services, user_email=args.email, channel="text")
    chat = TextChat(session, chat_client(settings), chat_model(settings))
    mode = "will create real incidents" if services.writer.live else "drafts tickets only (dry run)"
    print(f"Help desk assistant, {mode}. Empty line or Ctrl+C to quit.\n")
    print(f"assistant> {chat.greeting}")

    async def loop() -> None:
        while True:
            try:
                text = await asyncio.to_thread(input, "you> ")
            except EOFError:
                break
            if not text.strip():
                break
            print("assistant> ", end="", flush=True)
            async for part in chat.reply(text):
                print(part, end="", flush=True)
            print()

    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        print()
    session.close()
    if session.incident is not None:
        if session.incident.created:
            print(f"Raised {session.incident.number}: {session.incident.url}")
        else:
            print("Dry run, nothing created. The incident would have been:")
            print(json.dumps(session.incident.payload, indent=2))
    print(f"Conversation {session.id} ended: {session.outcome}")
    return 0


def _index(args, settings) -> int:
    from .sources.ticket_index import TicketIndex

    index = TicketIndex(settings.app_db_path)
    if args.action == "status":
        stats = index.stats()
        print(f"{stats['tickets']} ticket(s) indexed in {settings.app_db_path}")
        print(f"  by table: {stats['by_table'] or '-'}")
        print(f"  by resolution: {stats['by_level'] or '-'}  (rich = a fix is written down)")
        if stats["unchecked"]:
            print(f"  {stats['unchecked']} still to be checked by the model on the next sync")
        print(f"  last sync: {stats['last_sync'] or 'never'}")
        print(f"  KNOWLEDGE_SOURCE={settings.knowledge_source}")
        return 0
    if args.action == "search":
        keys = index.search_text(args.text, limit=args.limit)
        if not keys:
            print("no matches")
        for key in keys:
            ticket, level = index.load([key])[key]
            print(f"{ticket.number:12} {level.value:5} {ticket.short_description[:70]}")
            if ticket.resolution_text:
                print(f"{'':18}fix: {ticket.resolution_text.splitlines()[0][:80]}")
        return 0

    import time

    from .intake.evidence import HumanNotesOnly
    from .llm import TcsGenAiLLM
    from .servicenow.client import ServiceNowClient
    from .sources.ticket_index import sync_index

    client = HumanNotesOnly(
        ServiceNowClient(settings), [a for a in settings.intake_agent_note_authors.split(",") if a.strip()]
    )
    llm = TcsGenAiLLM(settings) if settings.tcs_genai_api_key.get_secret_value() else None
    if llm is None:
        log.warning("no LLM key set: unclear tickets get the safe rule-based level until a later sync")
    full = args.full
    while True:
        report = sync_index(index, client, settings, llm, full=full, days=args.days)
        print(
            f"stored {report.stored}, removed {report.removed} (reopened), "
            f"rechecked {report.rechecked}; {index.count()} ticket(s) in the index"
        )
        if not args.watch:
            return 0
        full = False
        time.sleep(args.watch * 60)


def _users(args, settings) -> int:
    import getpass

    from .recorder import open_store
    from .web.auth import hash_password

    store = open_store(settings)
    if args.action == "list":
        rows = store.list_users()
        if not rows:
            print("No logins yet. Add one with: sn-agent users add EMAIL --role ops")
        for row in rows:
            print(f"  {row['role']:<4} {row['email']:<36} {row['name']}")
        return 0
    if args.action == "remove":
        print("removed" if store.delete_user(args.email) else "no such login")
        return 0
    password = getpass.getpass(f"Password for {args.email}: ")
    if len(password) < 8:
        print("Use at least 8 characters.")
        return 2
    if getpass.getpass("Again: ") != password:
        print("The two passwords differ.")
        return 2
    store.put_user(args.email, args.name, args.role, hash_password(password))
    print(f"Saved {args.email} ({args.role}).")
    return 0


def _run_without_webhook(settings) -> int:
    """Workers and the sweep, with no web server at all. Every connection is
    outbound, to ServiceNow and the TCS GenAI Lab gateway; nothing on the network can connect in."""

    import time

    from .runner import JobRunner
    from .servicenow.client import ServiceNowClient
    from .sweep import Sweeper

    runner = JobRunner(workers=settings.worker_threads, max_failures=settings.max_failures_per_ticket)
    sweeper = Sweeper(runner, settings, lambda: ServiceNowClient(settings))
    monitor = None
    if settings.record_agent_runs:
        from .monitor import AgentMonitor

        # Before the workers start, so their first lines reach the live log too.
        monitor = AgentMonitor(runner, settings, sweeper).start()
    runner.start()
    sweeper.start()
    print(
        f"Running: {settings.worker_threads} workers, sweep every {settings.sweep_interval_minutes:g} min "
        f"over the last {settings.sweep_window_days} days. No port is open. Ctrl+C to stop."
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        if monitor is not None:
            monitor.stop()
        print("Stopped.")
    return 0


def _sweep(args, settings) -> int:
    from .factory import build_service
    from .runner import JobRunner
    from .servicenow.client import ServiceNowClient
    from .sweep import find_pending, queue_pending

    pending = find_pending(ServiceNowClient(settings), settings)[: args.limit]
    if not pending:
        print(f"Nothing to do: every open ticket from the last {settings.sweep_window_days} days has a current note.")
        return 0

    print(f"{len(pending)} ticket(s) need the agent, most urgent first:\n")
    print(f"  {'#':>2}  {'P':<2} {'ticket':<11} {'opened':<16} {'why':<14} title")
    for index, item in enumerate(pending, start=1):
        opened = item.opened_at.strftime("%Y-%m-%d %H:%M") if item.opened_at else "-"
        print(f"  {index:>2}  P{item.priority} {item.number:<11} {opened:<16} {item.reason:<14} {item.short_description[:50]}")
    if args.list:
        return 0

    workers = args.workers or settings.worker_threads
    mode = "dry run, nothing posted" if args.dry_run else "posting notes"
    print(f"\nWorking them with {workers} worker(s) ({mode})...\n")
    runner = JobRunner(
        workers=workers,
        service_factory=lambda: build_service(settings, dry_run=args.dry_run),
        max_failures=settings.max_failures_per_ticket,
    )
    runner.start()
    queue_pending(runner, pending)
    runner.wait()

    total = 0.0
    for result in sorted(runner.results, key=lambda r: (r.priority, r.number)):
        if result.status in {"posted", "printed"}:
            total += result.cost_usd
            print(
                f"  P{result.priority} {result.number:<11} {result.status:<8} "
                f"{result.outcome:<32} {result.confidence:>5.0f}/100  ${result.cost_usd:.4f}"
            )
        else:
            print(f"  P{result.priority} {result.number:<11} {result.status:<8} {result.detail}")
    print(f"\nTotal cost: ${total:.4f}")
    return 0 if all(r.status != "failed" for r in runner.results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
