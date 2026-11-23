import json
from concurrent.futures import ThreadPoolExecutor

from .model import Session, SourceError
from .runner import Runner, output


def objects(data: dict, key: str) -> list[dict]:
    rows = data.get(key, [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceError(f"invalid snapshot field {key}")
    return rows


def collect(runner: Runner) -> list[Session]:
    try:
        snapshot = json.loads(output(runner, ["herdr", "api", "snapshot"])).get("result", {}).get("snapshot")
    except (ValueError, AttributeError) as exc:
        raise SourceError("invalid snapshot response") from exc
    if not isinstance(snapshot, dict):
        raise SourceError("missing snapshot")

    spaces = {w.get("workspace_id"): w for w in objects(snapshot, "workspaces")}
    tabs = {t.get("tab_id"): t for t in objects(snapshot, "tabs")}
    panes = objects(snapshot, "panes")
    if any(not p.get("pane_id") or not p.get("workspace_id") for p in panes):
        raise SourceError("missing pane_id or workspace_id")

    def pane_name(pane):
        tab = tabs.get(pane.get("tab_id"), {})
        return pane.get("label") or pane.get("title") or tab.get("label") or pane.get("pane_id")

    def number(row):
        value = row.get("number")
        return value if isinstance(value, int) else float("inf")

    def order(pane):
        wid = pane.get("workspace_id")
        tab = tabs.get(pane.get("tab_id"), {})
        return number(spaces.get(wid, {})), wid, number(tab), pane_name(pane), pane.get("pane_id")

    def command(pane):
        try:
            raw = output(runner, ["herdr", "pane", "process-info", "--pane", pane.get("pane_id")])
            info = json.loads(raw).get("result", {}).get("process_info", {})
            return info.get("foreground_processes", [])[0].get("cmdline") or None
        except (SourceError, ValueError, AttributeError, IndexError, KeyError, TypeError):
            return None

    sessions = []
    seen = set()
    panes = sorted(panes, key=order)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for p, cmd in zip(panes, pool.map(command, panes)):
            pid, wid = p.get("pane_id"), p.get("workspace_id")
            name = pane_name(p)
            key = wid, name
            if key in seen:
                name = f"{name}#{pid}"
            seen.add(key)
            status = p.get("agent_status")
            sessions.append(Session(
                "herdr", spaces.get(wid, {}).get("label") or wid, name, pid,
                p.get("display_agent") or p.get("agent") or None,
                status if status in ("working", "blocked", "done", "idle") else "unknown",
                cmd, p.get("foreground_cwd") or p.get("cwd"), bool(p.get("focused")),
            ))
    return sessions
