"""greffierctl — command-line client of the Greffier service.

Every command talks to the daemon over D-Bus; the daemon is started on demand
by D-Bus activation, so ``greffierctl toggle`` can be bound to any key.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from greffier.const import APP_NAME, DBUS_IFACE, DBUS_NAME, DBUS_PATH, VERSION
from greffier.i18n import _

TIMEOUT_MS = 30_000


class CliError(Exception):
    pass


class Client:
    def __init__(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            raise CliError(_("Cannot connect to the session bus: {error}").format(
                error=exc.message)) from exc

    def call(self, method: str, signature: str | None = None, *args: Any) -> Any:
        params = GLib.Variant(signature, args) if signature else None
        try:
            reply = self.bus.call_sync(DBUS_NAME, DBUS_PATH, DBUS_IFACE, method, params, None,
                                       Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
        except GLib.Error as exc:
            message = Gio.DBusError.strip_remote_error(exc) or exc.message
            raise CliError(message) from exc
        values = reply.unpack() if reply is not None else ()
        return values[0] if values else None

    def json(self, method: str, signature: str | None = None, *args: Any) -> Any:
        return json.loads(self.call(method, signature, *args))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_simple(client: Client, args: argparse.Namespace) -> int:
    client.call(args.method)
    return 0


def cmd_status(client: Client, args: argparse.Namespace) -> int:
    status = client.json("GetStatus")
    if args.json:
        print_json(status)
        return 0
    print(_("State: {state}").format(state=status["state"]))
    if status.get("detail"):
        print(f"  {status['detail']}")
    model = status.get("model_name") or _("none")
    loaded = _("loaded") if status.get("model_loaded") else _("not loaded")
    print(_("Model: {model} ({loaded})").format(model=model, loaded=loaded))
    sc = status.get("shortcut", {})
    state = _("active") if sc.get("active") else _("inactive")
    print(_("Shortcut: {accel} — {mode}, {backend}, {state}").format(
        accel=sc.get("accelerator", "?"), mode=sc.get("mode", "?"),
        backend=sc.get("backend", "?"), state=state))
    if sc.get("message"):
        print(f"  {sc['message']}")
    inj = status.get("injection", {})
    print(_("Text insertion: {method} (keyboard: {keyboard}, clipboard: {clipboard})").format(
        method=inj.get("method", "?"), keyboard=inj.get("keyboard", "?"),
        clipboard=inj.get("clipboard", "?")))
    return 0


def cmd_models(client: Client, args: argparse.Namespace) -> int:
    action = args.action
    if action in (None, "list"):
        models = client.json("ListModels")
        if args.json:
            print_json(models)
            return 0
        for m in models:
            if m.get("kind") != "asr" and not args.all:
                continue
            flags = []
            if m.get("active"):
                flags.append(_("active"))
            if m.get("installed"):
                flags.append(_("installed"))
            if m.get("update_available"):
                flags.append(_("update available"))
            if m.get("recommended"):
                flags.append(_("recommended"))
            if m.get("install"):
                flags.append(_("installing"))
            mark = "●" if m.get("active") else ("○" if m.get("installed") else " ")
            size = human_size(m["download"]["size"])
            print(f"{mark} {m['id']:<34} {m['name']:<26} {size:>9}  {', '.join(flags)}")
        return 0
    if action == "install":
        if args.no_wait:
            client.call("InstallModel", "(s)", args.id)
            return 0
        return install_and_wait(client, args.id)
    if action == "remove":
        client.call("RemoveModel", "(s)", args.id)
        print(_("{id} removed.").format(id=args.id))
        return 0
    if action == "use":
        client.call("SetConfig", "(s)", json.dumps({"model": {"active": args.id}}))
        print(_("Active model: {id}").format(id=args.id))
        return 0
    if action == "cancel":
        client.call("CancelInstall", "(s)", args.id)
        return 0
    if action == "check":
        client.call("CheckUpdates")
        print(_("Checking for updates in the background; see `greffierctl models list`."))
        return 0
    raise CliError(_("Unknown action {action}").format(action=action))


def install_and_wait(client: Client, model_id: str) -> int:
    """Start an install and follow its ModelProgress signals until it ends."""
    loop = GLib.MainLoop()
    outcome = {"code": 1}
    tty = sys.stdout.isatty()

    def on_signal(_bus, _sender, _path, _iface, _signal, params) -> None:
        mid, phase, done, total, message = params.unpack()
        if mid != model_id:
            return
        if phase in ("download", "verify", "extract"):
            pct = 100 * done / total if total else 0
            label = {"download": _("Downloading"), "verify": _("Verifying"),
                     "extract": _("Extracting")}[phase]
            line = f"{label} {pct:5.1f} %  ({human_size(done)} / {human_size(total)})"
            print(f"\r{line:<60}" if tty else line, end="" if tty else "\n", flush=True)
            return
        if tty:
            print()
        if phase == "done":
            print(_("{id} installed.").format(id=model_id))
            outcome["code"] = 0
        elif phase == "cancelled":
            print(_("Installation cancelled."))
        else:
            print(_("Installation failed: {error}").format(error=message), file=sys.stderr)
        loop.quit()

    # Subscribe before asking for the install: a fast install could otherwise
    # finish before we listen and we would wait forever.
    sub = client.bus.signal_subscribe(None, DBUS_IFACE, "ModelProgress", DBUS_PATH, None,
                                      Gio.DBusSignalFlags.NONE, on_signal)

    def on_vanished(_bus, _name) -> None:
        if tty:
            print()
        print(_("The Greffier service stopped during the installation."), file=sys.stderr)
        loop.quit()

    watch = 0
    try:
        client.call("InstallModel", "(s)", model_id)
        # Only watch once the call (and any D-Bus activation) succeeded.
        watch = Gio.bus_watch_name_on_connection(client.bus, DBUS_NAME,
                                                 Gio.BusNameWatcherFlags.NONE, None,
                                                 on_vanished)
        loop.run()
    except KeyboardInterrupt:
        client.call("CancelInstall", "(s)", model_id)
        print()
        return 130
    finally:
        client.bus.signal_unsubscribe(sub)
        if watch:
            Gio.bus_unwatch_name(watch)
    return outcome["code"]


def cmd_config(client: Client, args: argparse.Namespace) -> int:
    if args.action in (None, "get"):
        cfg = client.json("GetConfig")
        if args.key:
            section, _sep, name = args.key.partition(".")
            value = cfg.get(section, {}).get(name) if name else cfg.get(section)
            if value is None:
                raise CliError(_("Unknown setting {key}").format(key=args.key))
            print_json(value)
        else:
            print_json(cfg)
        return 0
    section, _sep, name = (args.key or "").partition(".")
    if not name or args.value is None:
        raise CliError(_("Usage: greffierctl config set section.key value"))
    client.call("SetConfig", "(s)", json.dumps({section: {name: parse_value(args.value)}}))
    return 0


def cmd_history(client: Client, args: argparse.Namespace) -> int:
    if args.action == "clear":
        client.call("ClearHistory")
        return 0
    items = client.json("GetHistory")[: args.n]
    if args.json:
        print_json(items)
        return 0
    for item in items:
        print(f"[{item.get('time', '')}] {item.get('text', '')}")
    return 0


def cmd_devices(client: Client, args: argparse.Namespace) -> int:
    devices = client.json("ListAudioDevices")
    if args.json:
        print_json(devices)
        return 0
    if not devices:
        print(_("No microphone found."))
    for d in devices:
        mark = "*" if d.get("is_default") else " "
        print(f"{mark} {d['name']}\n    {d['id']}")
    return 0


def cmd_doctor(client: Client, args: argparse.Namespace) -> int:
    report = client.json("GetDiagnostics")
    if args.json:
        print_json(report)
        return 0
    parts = [
        f"{APP_NAME} {report.get('version', '?')}",
        f"GNOME Shell {report.get('gnome_shell') or '?'}",
        report.get("session_type") or "?",
        f"sherpa-onnx {report.get('sherpa_onnx') or '?'}",
    ]
    print(" — ".join(parts))
    icons = {"ok": "✔", "warning": "!", "error": "✘"}
    worst = 0
    for check in report.get("checks", []):
        level = check.get("level", "ok")
        worst = max(worst, {"ok": 0, "warning": 0, "error": 1}.get(level, 0))
        print(f" {icons.get(level, '?')} {check.get('message', '')}")
    return worst


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="greffierctl",
                                     description=_("Control the Greffier dictation service."))
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    for name, method, text in (
        ("toggle", "Toggle", _("start or stop dictation")),
        ("start", "Start", _("start dictation")),
        ("stop", "Stop", _("stop and transcribe")),
        ("cancel", "Cancel", _("stop and discard the recording")),
        ("quit", "Quit", _("stop the service")),
    ):
        p = sub.add_parser(name, help=text)
        p.set_defaults(func=cmd_simple, method=method)

    p = sub.add_parser("status", help=_("show the current state"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", help=_("list, install, update or remove models"))
    p.add_argument("action", nargs="?",
                   choices=["list", "install", "remove", "use", "cancel", "check"])
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help=_("include support models"))
    p.add_argument("--no-wait", action="store_true", help=_("do not wait for the download"))
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("config", help=_("read or change settings"))
    p.add_argument("action", nargs="?", choices=["get", "set"])
    p.add_argument("key", nargs="?", help="section.key")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("history", help=_("show recent dictations"))
    p.add_argument("action", nargs="?", choices=["show", "clear"])
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("devices", help=_("list microphones"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("doctor", help=_("check the installation"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    if args.func is cmd_models and args.action in ("install", "remove", "use", "cancel") \
            and not args.id:
        parser.error(_("a model id is required"))
    try:
        return args.func(Client(), args)
    except CliError as exc:
        print(_("Error: {error}").format(error=exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
