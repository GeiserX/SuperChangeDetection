"""scd: the command line. `check` proves the settings and the changedetection connection;
`watches` lists them as the AI sees them; `apply` runs an actions block from a file, the same path
the chat uses, so a watch can be created or fixed without the bot."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, actions, context, render
from .cd import CD, CDError
from .i18n import Translator
from .model import Data


def _cd(data: Data) -> CD:
    cfg = data.settings.changedetection
    key = data.secret(cfg.api_key_env)
    if not key:
        sys.exit(f"{cfg.api_key_env} is not set")
    return CD(cfg.url, key, cfg.timeout_s)


def cmd_check(data: Data, args) -> int:
    print(f"settings: {data.root / 'settings.yaml'} (locale {data.settings.locale})")
    cd = _cd(data)
    try:
        info = cd.systeminfo()
    except CDError as e:
        print(f"changedetection: {e}")
        return 1
    print(
        f"changedetection: {data.settings.changedetection.url} v{info.get('version')} "
        f"({info.get('watch_count')} watches)"
    )
    token = data.secret(data.settings.bot.token_env)
    print(f"telegram token: {'set' if token else 'NOT set'}")
    print(f"llm: {data.settings.llm.provider}")
    return 0


def cmd_watches(data: Data, args) -> int:
    cd = _cd(data)
    t = Translator(data.settings.locale)
    token = data.secret(data.settings.bot.token_env)
    print(context.facts(cd, token, data.settings, t, args.chat))
    return 0


def cmd_rules(data: Data, args) -> int:
    print(context.rules(data.settings, Translator(data.settings.locale)))
    return 0


def cmd_apply(data: Data, args) -> int:
    cd = _cd(data)
    t = Translator(data.settings.locale)
    token = data.secret(data.settings.bot.token_env)
    text = Path(args.file).read_text(encoding="utf-8")
    if "```" not in text:
        text = "```actions\n" + text + "\n```"
    _, acts = actions.extract(text)
    if not acts:
        sys.exit("no actions in the file")
    actions.resolve(acts, [(n, u) for n, u, _ in context.listing(cd, token)])
    chat = args.chat if args.chat is not None else (data.settings.bot.chat_id or 0)
    rs = actions.apply(cd, acts, data.settings, token, chat, t)
    print(render.results(rs, t))
    return 0 if all(r.ok for r in rs) else 1


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="scd", description="SuperChangeDetection")
    p.add_argument("--data", default=os.environ.get("SCD_DATA", "data"))
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="settings and connection check")
    w = sub.add_parser("watches", help="the watches as the AI sees them")
    w.add_argument("--chat", type=int, default=None)
    sub.add_parser("rules", help="print the AI's rules")
    a = sub.add_parser("apply", help="apply an actions block from a YAML file")
    a.add_argument("file")
    a.add_argument("--chat", type=int, default=None, help="the chat the watch notifies")
    args = p.parse_args(argv)
    data = Data(Path(args.data))
    code = {"check": cmd_check, "watches": cmd_watches, "rules": cmd_rules, "apply": cmd_apply}[
        args.cmd
    ](data, args)
    sys.exit(code)
