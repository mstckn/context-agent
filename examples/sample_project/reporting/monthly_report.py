"""Monthly usage report generation.

Large module on purpose: exercises the big-file preview path
(symbol map + get_range) instead of dumping the whole file.
"""

import json
from datetime import date, timedelta
from pathlib import Path


def start_of_month(day):
    return day.replace(day=1)


def end_of_month(day):
    if day.month == 12:
        return day.replace(day=31)
    return start_of_month(day.replace(month=day.month + 1)) - timedelta(days=1)


def month_key(day):
    return day.strftime("%Y-%m")


def load_events(events_path):
    path = Path(events_path)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def filter_events_by_month(events, day):
    key = month_key(day)
    return [e for e in events if str(e.get("ts", ""))[:7] == key]


def event_user(event):
    return event.get("user") or "anonymous"


def event_kind(event):
    return event.get("kind") or "unknown"


def count_events(events):
    return len(events)


def count_users(events):
    return len({event_user(e) for e in events})


def events_per_user(events):
    per_user = {}
    for event in events:
        user = event_user(event)
        per_user[user] = per_user.get(user, 0) + 1
    return per_user


def top_users(events, limit=10):
    ranked = sorted(events_per_user(events).items(), key=lambda kv: -kv[1])
    return ranked[:limit]


def events_per_kind(events):
    per_kind = {}
    for event in events:
        kind = event_kind(event)
        per_kind[kind] = per_kind.get(kind, 0) + 1
    return per_kind


def top_kinds(events, limit=10):
    ranked = sorted(events_per_kind(events).items(), key=lambda kv: -kv[1])
    return ranked[:limit]


def daily_counts(events):
    counts = {}
    for event in events:
        day = str(event.get("ts", ""))[:10]
        counts[day] = counts.get(day, 0) + 1
    return dict(sorted(counts.items()))


def peak_day(events):
    counts = daily_counts(events)
    if not counts:
        return None, 0
    day, total = max(counts.items(), key=lambda kv: kv[1])
    return day, total


def quiet_day(events):
    counts = daily_counts(events)
    if not counts:
        return None, 0
    day, total = min(counts.items(), key=lambda kv: kv[1])
    return day, total


def average_per_day(events):
    counts = daily_counts(events)
    if not counts:
        return 0.0
    return round(sum(counts.values()) / len(counts), 2)


def error_events(events):
    return [e for e in events if event_kind(e) == "error"]


def error_rate(events):
    total = count_events(events)
    if not total:
        return 0.0
    return round(len(error_events(events)) / total, 4)


def auth_events(events):
    return [e for e in events if event_kind(e) in {"login", "logout", "token"}]


def payment_events(events):
    return [e for e in events if event_kind(e) in {"invoice", "refund", "charge"}]


def refund_events(events):
    return [e for e in events if event_kind(e) == "refund"]


def refund_ratio(events):
    payments = payment_events(events)
    if not payments:
        return 0.0
    return round(len(refund_events(events)) / len(payments), 4)


def new_users(events, known_users):
    seen = set(known_users or [])
    fresh = []
    for event in events:
        user = event_user(event)
        if user not in seen and user != "anonymous":
            seen.add(user)
            fresh.append(user)
    return fresh


def retention_pairs(previous_events, current_events):
    prev_users = {event_user(e) for e in previous_events}
    curr_users = {event_user(e) for e in current_events}
    return sorted(prev_users & curr_users)


def retention_rate(previous_events, current_events):
    prev_users = {event_user(e) for e in previous_events}
    if not prev_users:
        return 0.0
    kept = retention_pairs(previous_events, current_events)
    return round(len(kept) / len(prev_users), 4)


def churned_users(previous_events, current_events):
    prev_users = {event_user(e) for e in previous_events}
    curr_users = {event_user(e) for e in current_events}
    return sorted(prev_users - curr_users)


def format_number(value):
    return f"{value:,}"


def format_percent(value):
    return f"{value * 100:.1f}%"


def section_header(title):
    return f"\n## {title}\n"


def render_summary(events):
    lines = [section_header("Summary")]
    lines.append(f"- events: {format_number(count_events(events))}")
    lines.append(f"- users: {format_number(count_users(events))}")
    lines.append(f"- avg/day: {average_per_day(events)}")
    return "\n".join(lines)


def render_top_users(events, limit=10):
    lines = [section_header("Top users")]
    for user, total in top_users(events, limit):
        lines.append(f"- {user}: {format_number(total)}")
    return "\n".join(lines)


def render_kinds(events):
    lines = [section_header("Event kinds")]
    for kind, total in top_kinds(events):
        lines.append(f"- {kind}: {format_number(total)}")
    return "\n".join(lines)


def render_health(events):
    lines = [section_header("Health")]
    lines.append(f"- error rate: {format_percent(error_rate(events))}")
    lines.append(f"- refund ratio: {format_percent(refund_ratio(events))}")
    peak, peak_total = peak_day(events)
    quiet, quiet_total = quiet_day(events)
    if peak:
        lines.append(f"- peak day: {peak} ({format_number(peak_total)})")
    if quiet:
        lines.append(f"- quiet day: {quiet} ({format_number(quiet_total)})")
    return "\n".join(lines)


def render_retention(previous_events, current_events):
    lines = [section_header("Retention")]
    lines.append(f"- rate: {format_percent(retention_rate(previous_events, current_events))}")
    kept = retention_pairs(previous_events, current_events)
    lines.append(f"- retained users: {len(kept)}")
    lost = churned_users(previous_events, current_events)
    lines.append(f"- churned users: {len(lost)}")
    return "\n".join(lines)


def build_report(events_path, day, previous_events=None):
    day = day or date.today()
    events = filter_events_by_month(load_events(events_path), day)
    parts = [
        f"# Monthly usage report {month_key(day)}",
        render_summary(events),
        render_top_users(events),
        render_kinds(events),
        render_health(events),
    ]
    if previous_events is not None:
        parts.append(render_retention(previous_events, events))
    return "\n".join(parts)


def write_report(events_path, out_path, day=None, previous_events=None):
    text = build_report(events_path, day, previous_events)
    Path(out_path).write_text(text, encoding="utf-8")
    return out_path
