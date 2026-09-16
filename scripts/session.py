#!/usr/bin/env python3
"""
session.py — Çalışma session yöneticisi.
LLM'in geçmiş kararları ve notları hatırlamasını sağlar.

Kullanım:
  python session.py --new "Auth refactor"          # yeni session
  python session.py --note "login'de rate limit yok, eklenecek"
  python session.py --decision "redis kullanacağız, memcached değil"
  python session.py --show                         # mevcut session
  python session.py --history                      # tüm session'lar
  python session.py --context                      # LLM'e gönderilecek özet
"""

import sys, json, argparse, re
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def find_root():
    try:
        from paths import find_project_root
    except ImportError:
        from scripts.paths import find_project_root
    return find_project_root()

def scope_key():
    try:
        from paths import resolve_runtime_scope
    except ImportError:
        from scripts.paths import resolve_runtime_scope
    return resolve_runtime_scope()

def project_meta():
    root = find_root()
    try:
        from paths import ensure_project_id
    except ImportError:
        from scripts.paths import ensure_project_id
    try:
        from git_meta import git_info
    except ImportError:
        from scripts.git_meta import git_info
    return {
        "project_id": ensure_project_id(root),
        "scope": scope_key(),
        "git": git_info(root),
    }

def sessions_dir():
    base = find_root() / ".context" / "sessions"
    scope = scope_key()
    if scope == "default":
        return base
    return base / scope

def current_session_file():
    return sessions_dir() / "current.json"

def load_current():
    f = current_session_file()
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None

def save_current(data):
    sessions_dir().mkdir(parents=True, exist_ok=True)
    current_session_file().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def archive_safe_name(name):
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(name or "session")).strip("_")
    return safe[:80] or "session"


def task_terms(task=""):
    return {
        word
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]+|\w+", str(task).lower())
        if len(word) >= 3
    }

def new_session(name):
    sessions_dir().mkdir(parents=True, exist_ok=True)

    # Eskiyi arşivle
    existing = load_current()
    if existing:
        ts = existing.get("started_at", "unknown").replace(":", "-")[:19]
        archive_name = f"{ts}_{archive_safe_name(existing.get('name', 'session'))}.json"
        archive_path = sessions_dir() / archive_name
        archive_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    session = {
        "name": name,
        "started_at": datetime.now().isoformat(),
        "notes": [],
        "decisions": [],
        "files_touched": [],
        "tasks_completed": [],
        "meta": project_meta(),
    }
    save_current(session)
    print(json.dumps({"status": "created", "name": name}, indent=2))

def add_note(text):
    session = load_current()
    if not session:
        session = {"name": "default", "started_at": datetime.now().isoformat(),
                   "notes": [], "decisions": [], "files_touched": [], "tasks_completed": []}

    session["notes"].append({
        "text": text,
        "at": datetime.now().isoformat(),
        "meta": project_meta(),
    })
    save_current(session)
    print(json.dumps({"status": "noted", "text": text}, indent=2))

def add_decision(text):
    session = load_current()
    if not session:
        new_session("auto")
        session = load_current()

    session["decisions"].append({
        "decision": text,
        "at": datetime.now().isoformat(),
        "meta": project_meta(),
    })
    save_current(session)
    print(json.dumps({"status": "recorded", "decision": text}, indent=2))

def touch_file(filepath, action="modified"):
    session = load_current()
    if not session:
        return

    # Aynı dosya tekrar gelirse güncelle
    existing = next((x for x in session["files_touched"] if x["file"] == filepath), None)
    if existing:
        existing["action"] = action
        existing["at"] = datetime.now().isoformat()
        existing["count"] = existing.get("count", 1) + 1
    else:
        session["files_touched"].append({
            "file": filepath,
            "action": action,
            "at": datetime.now().isoformat(),
            "count": 1,
            "meta": project_meta(),
        })
    save_current(session)
    print(json.dumps({"status": "tracked", "file": filepath, "action": action}))

def complete_task(text):
    session = load_current()
    if not session:
        return
    session["tasks_completed"].append({
        "task": text,
        "at": datetime.now().isoformat(),
        "meta": project_meta(),
    })
    save_current(session)
    print(json.dumps({"status": "completed", "task": text}))

def show_session():
    session = load_current()
    if not session:
        print(json.dumps({"status": "no_session", "hint": "python .context/scripts/session.py --new \"Task adı\""}))
        return
    print(json.dumps(session, indent=2, ensure_ascii=False))

def get_context(task=""):
    """LLM'e gönderilecek kısa session özeti"""
    session = load_current()
    if not session:
        recent = recent_session_context(task=task)
        if recent:
            print(json.dumps(recent, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"context": "Aktif session yok."}))
        return

    lines = [f"# Session: {session['name']}"]
    lines.append(f"Başladı: {session['started_at'][:10]}")

    if session.get("decisions"):
        lines.append("\n## Kararlar:")
        for d in session["decisions"][-5:]:
            lines.append(f"- {d['decision']}")

    if session.get("notes"):
        lines.append("\n## Notlar:")
        for n in session["notes"][-5:]:
            lines.append(f"- {n['text']}")

    if session.get("files_touched"):
        lines.append("\n## Değiştirilen dosyalar:")
        for f in session["files_touched"][-8:]:
            lines.append(f"- {f['file']} ({f['action']})")

    if session.get("tasks_completed"):
        lines.append("\n## Tamamlanan:")
        for t in session["tasks_completed"][-3:]:
            lines.append(f"- [done] {t['task']}")

    recent = recent_session_lines(exclude_started_at=session.get("started_at"), limit=3, task=task or session.get("name", ""))
    if recent:
        lines.append("\n## Yakın Geçmiş Sessionlar:")
        lines.extend(recent)

    context_text = "\n".join(lines)
    token_est = int(len(context_text.split()) * 1.3)

    print(json.dumps({
        "context": context_text,
        "token_estimate": token_est
    }, indent=2, ensure_ascii=False))


def iter_archived_sessions():
    d = sessions_dir()
    if not d.exists():
        return []
    items = []
    for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name == "current.json" or f.name.startswith("stable_"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append(data)
        except Exception:
            pass
    return items


def recent_session_lines(exclude_started_at=None, limit=3, task=""):
    candidates = []
    current_git = project_meta().get("git", {})
    terms = task_terms(task)
    for data in iter_archived_sessions():
        if exclude_started_at and data.get("started_at") == exclude_started_at:
            continue
        name = data.get("name", "session")
        date = (data.get("started_at", "") or "")[:10]
        data_git = (data.get("meta") or {}).get("git", {})
        branch_note = ""
        if (
            current_git.get("available")
            and data_git.get("available")
            and data_git.get("branch") != current_git.get("branch")
        ):
            branch_note = f" [branch:{data_git.get('branch')} != current:{current_git.get('branch')}]"
        decisions = [d.get("decision", "") for d in data.get("decisions", []) if d.get("decision")]
        notes = [n.get("text", "") for n in data.get("notes", []) if n.get("text")]
        touched = [f.get("file", "") for f in data.get("files_touched", []) if f.get("file")]
        summary = "; ".join([*decisions[-2:], *notes[-1:]])[:220]
        file_hint = ", ".join(touched[-3:])
        detail = summary or (f"files: {file_hint}" if file_hint else "no compact notes")
        line = f"- {date} {name}{branch_note}: {detail}"
        haystack = f"{name} {detail} {file_hint}".lower()
        score = sum(1 for term in terms if term in haystack)
        candidates.append((score, data.get("started_at", ""), line))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [line for _, _, line in candidates[:limit]]


def recent_session_context(limit=3, task=""):
    lines = recent_session_lines(limit=limit, task=task)
    if not lines:
        return None
    text = "# Recent Sessions\n" + "\n".join(lines)
    return {"context": text, "token_estimate": int(len(text.split()) * 1.3)}

def history():
    d = sessions_dir()
    if not d.exists():
        print(json.dumps({"sessions": []}))
        return

    files = sorted(d.glob("*.json"))
    sessions = []
    for f in files:
        if f.name == "current.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append({
                "name": data.get("name"),
                "started": data.get("started_at","")[:10],
                "notes": len(data.get("notes",[])),
                "decisions": len(data.get("decisions",[])),
                "files": len(data.get("files_touched",[]))
            })
        except:
            pass

    print(json.dumps({"sessions": sessions, "count": len(sessions)}, indent=2))

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--new", metavar="NAME")
    group.add_argument("--note", metavar="TEXT")
    group.add_argument("--decision", metavar="TEXT")
    group.add_argument("--touch", nargs="+", metavar=("FILE","ACTION"))
    group.add_argument("--done", metavar="TASK")
    group.add_argument("--show", action="store_true")
    group.add_argument("--context", action="store_true")
    group.add_argument("--history", action="store_true")
    parser.add_argument("--task", default="", help="Optional task string for relevance-ranked memory.")
    args = parser.parse_args()

    if args.new:       new_session(args.new)
    elif args.note:    add_note(args.note)
    elif args.decision:add_decision(args.decision)
    elif args.touch:
        f = args.touch[0]
        action = args.touch[1] if len(args.touch) > 1 else "modified"
        touch_file(f, action)
    elif args.done:    complete_task(args.done)
    elif args.show:    show_session()
    elif args.context: get_context(args.task)
    elif args.history: history()

if __name__ == "__main__":
    main()
