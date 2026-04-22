#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.telegram_common import load_access, save_access


def cmd_show(_: argparse.Namespace) -> None:
    print(json.dumps(load_access(), indent=2))


def cmd_allow(args: argparse.Namespace) -> None:
    access = load_access()
    sender = str(args.user_id)
    if sender not in access["allowFrom"]:
        access["allowFrom"].append(sender)
    save_access(access)
    print(f"Allowed {sender}.")


def cmd_remove(args: argparse.Namespace) -> None:
    access = load_access()
    sender = str(args.user_id)
    access["allowFrom"] = [value for value in access["allowFrom"] if value != sender]
    save_access(access)
    print(f"Removed {sender}.")


def cmd_policy(args: argparse.Namespace) -> None:
    access = load_access()
    access["dmPolicy"] = args.policy
    save_access(access)
    print(f"dmPolicy set to {args.policy}.")


def cmd_pair(args: argparse.Namespace) -> None:
    access = load_access()
    entry = access["pending"].pop(args.code, None)
    if not entry:
        raise SystemExit(f"No pending pairing code {args.code!r}.")
    sender_id = str(entry["senderId"])
    if sender_id not in access["allowFrom"]:
        access["allowFrom"].append(sender_id)
    save_access(access)
    print(f"Paired {sender_id} from code {args.code}.")


def cmd_deny(args: argparse.Namespace) -> None:
    access = load_access()
    entry = access["pending"].pop(args.code, None)
    save_access(access)
    if entry:
        print(f"Denied pending code {args.code}.")
    else:
        print(f"No pending pairing code {args.code}.")


def cmd_group_add(args: argparse.Namespace) -> None:
    access = load_access()
    allow_from = [value.strip() for value in (args.allow or "").split(",") if value.strip()]
    access.setdefault("groups", {})
    access["groups"][str(args.group_id)] = {
        "requireMention": not args.no_mention,
        "allowFrom": allow_from,
    }
    save_access(access)
    print(f"Enabled group {args.group_id}.")


def cmd_group_rm(args: argparse.Namespace) -> None:
    access = load_access()
    access.setdefault("groups", {}).pop(str(args.group_id), None)
    save_access(access)
    print(f"Removed group {args.group_id}.")


def cmd_set_mentions(args: argparse.Namespace) -> None:
    access = load_access()
    access["mentionPatterns"] = list(args.patterns)
    save_access(access)
    print("Updated mention patterns.")


def cmd_set(args: argparse.Namespace) -> None:
    access = load_access()
    key = args.key
    raw_value = args.value

    if key == "ackReaction":
        access[key] = raw_value
    elif key == "replyToMode":
        if raw_value not in {"first", "all", "off"}:
            raise SystemExit("replyToMode must be one of: first, all, off.")
        access[key] = raw_value
    elif key == "textChunkLimit":
        value = int(raw_value)
        if value < 256 or value > 4096:
            raise SystemExit("textChunkLimit must be between 256 and 4096.")
        access[key] = value
    elif key == "chunkMode":
        if raw_value not in {"newline", "length"}:
            raise SystemExit("chunkMode must be one of: newline, length.")
        access[key] = raw_value
    elif key == "mentionPatterns":
        parsed = json.loads(raw_value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise SystemExit("mentionPatterns must be a JSON array of strings.")
        access[key] = parsed
    else:
        raise SystemExit(f"Unsupported key {key!r}.")

    save_access(access)
    print(f"Updated {key}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Codex Telegram bridge access.")
    subparsers = parser.add_subparsers(required=True)

    show = subparsers.add_parser("show")
    show.set_defaults(func=cmd_show)

    allow = subparsers.add_parser("allow")
    allow.add_argument("user_id")
    allow.set_defaults(func=cmd_allow)

    remove = subparsers.add_parser("remove")
    remove.add_argument("user_id")
    remove.set_defaults(func=cmd_remove)

    policy = subparsers.add_parser("policy")
    policy.add_argument("policy", choices=["pairing", "allowlist", "disabled"])
    policy.set_defaults(func=cmd_policy)

    pair = subparsers.add_parser("pair")
    pair.add_argument("code")
    pair.set_defaults(func=cmd_pair)

    deny = subparsers.add_parser("deny")
    deny.add_argument("code")
    deny.set_defaults(func=cmd_deny)

    group_add = subparsers.add_parser("group-add")
    group_add.add_argument("group_id")
    group_add.add_argument("--no-mention", action="store_true")
    group_add.add_argument("--allow", help="Comma-separated sender IDs")
    group_add.set_defaults(func=cmd_group_add)

    group_rm = subparsers.add_parser("group-rm")
    group_rm.add_argument("group_id")
    group_rm.set_defaults(func=cmd_group_rm)

    mentions = subparsers.add_parser("set-mentions")
    mentions.add_argument("patterns", nargs="*")
    mentions.set_defaults(func=cmd_set_mentions)

    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("key", choices=["ackReaction", "replyToMode", "textChunkLimit", "chunkMode", "mentionPatterns"])
    set_parser.add_argument("value")
    set_parser.set_defaults(func=cmd_set)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
