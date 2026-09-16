#!/usr/bin/env python3
"""
budget.py — Token bütçe yöneticisi.
Her script çağrısının token maliyetini takip eder, bütçe uyarısı verir.

Kullanım:
  python budget.py --init 8000          # session başlat, 8k limit
  python budget.py --spend 150 "login fonksiyonu okundu"
  python budget.py --status
  python budget.py --estimate "src/auth/service.py"
  python budget.py --reset
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

try:
    from safe_io import atomic_write_json, file_lock
except ImportError:
    atomic_write_json = None
    file_lock = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

COSTS_PER_1K = {
    "claude-sonnet": 0.003,
    "claude-haiku":  0.00025,
    "gemini-flash":  0.0001,
    "ollama":        0.00,
}

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

def budget_path():
    root = find_root()
    ctx = root / ".context"
    ctx.mkdir(exist_ok=True)
    scope = scope_key()
    if scope == "default":
        return ctx / "budget.json"
    scoped_dir = ctx / "budgets"
    scoped_dir.mkdir(parents=True, exist_ok=True)
    return scoped_dir / f"{scope}.json"

def load_budget():
    path = budget_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "max_tokens": 8000,
        "used_tokens": 0,
        "model": "claude-haiku",
        "entries": [],
        "started_at": datetime.now().isoformat()
    }

def save_budget(data):
    path = budget_path()
    if atomic_write_json:
        atomic_write_json(path, data)
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def budget_lock():
    root = find_root()
    lock_path = root / ".context" / f"budget-{scope_key()}.lock"
    return file_lock(lock_path) if file_lock else None

def init_budget(max_tokens, model="claude-haiku"):
    lock = budget_lock()
    if lock:
        with lock:
            data = {
                "max_tokens": max_tokens,
                "used_tokens": 0,
                "model": model,
                "entries": [],
                "started_at": datetime.now().isoformat()
            }
            save_budget(data)
    else:
        data = {
            "max_tokens": max_tokens,
            "used_tokens": 0,
            "model": model,
            "entries": [],
            "started_at": datetime.now().isoformat()
        }
        save_budget(data)
    print(json.dumps({
        "status": "initialized",
        "scope": scope_key(),
        "max_tokens": max_tokens,
        "model": model,
        "cost_per_1k": COSTS_PER_1K.get(model, 0)
    }, indent=2))

def spend(tokens, reason=""):
    lock = budget_lock()
    if lock:
        with lock:
            return spend_locked(tokens, reason)
    return spend_locked(tokens, reason)

def spend_locked(tokens, reason=""):
    data = load_budget()
    remaining_before = data["max_tokens"] - data["used_tokens"]

    if tokens > remaining_before:
        print(json.dumps({
            "status": "budget_exceeded",
            "requested": tokens,
            "remaining": remaining_before,
            "warning": f"Bütçe aşılacak! {tokens} token istendi, {remaining_before} kaldı.",
            "suggestion": "Daha küçük bir parça iste veya get_symbol ile sadece fonksiyon body'si al."
        }, indent=2))
        return

    data["used_tokens"] += tokens
    data["entries"].append({
        "tokens": tokens,
        "reason": reason,
        "at": datetime.now().isoformat()
    })

    remaining = data["max_tokens"] - data["used_tokens"]
    pct_used = data["used_tokens"] / data["max_tokens"] * 100

    # Uyarı seviyeleri
    warning = None
    if pct_used >= 90:
        warning = "KRİTİK: Bütçe %90 doldu. Sadece zorunlu context isteyin."
    elif pct_used >= 75:
        warning = "UYARI: Bütçe %75 doldu. Context seçimini daraltın."
    elif pct_used >= 50:
        warning = "BİLGİ: Bütçe yarılandı."

    # Maliyet hesabı
    cost_per_1k = COSTS_PER_1K.get(data.get("model", "claude-haiku"), 0)
    total_cost = data["used_tokens"] / 1000 * cost_per_1k

    result = {
        "status": "ok",
        "spent": tokens,
        "reason": reason,
        "used": data["used_tokens"],
        "remaining": remaining,
        "percent_used": round(pct_used, 1),
        "estimated_cost_usd": round(total_cost, 6)
    }
    if warning:
        result["warning"] = warning

    save_budget(data)
    print(json.dumps(result, indent=2))

def status():
    data = load_budget()
    used = data["used_tokens"]
    maximum = data["max_tokens"]
    remaining = maximum - used
    pct = used / maximum * 100

    model = data.get("model", "claude-haiku")
    cost_per_1k = COSTS_PER_1K.get(model, 0)
    total_cost = used / 1000 * cost_per_1k

    # Son 5 harcama
    last_entries = data.get("entries", [])[-5:]

    bar_filled = int(pct / 5)
    bar = "#" * bar_filled + "." * (20 - bar_filled)

    print(json.dumps({
        "scope": scope_key(),
        "model": model,
        "used": used,
        "max": maximum,
        "remaining": remaining,
        "percent_used": round(pct, 1),
        "bar": f"[{bar}] {pct:.0f}%",
        "estimated_cost_usd": round(total_cost, 6),
        "cost_per_1k_tokens": cost_per_1k,
        "status": "critical" if pct >= 90 else "warning" if pct >= 75 else "ok",
        "last_entries": last_entries
    }, indent=2))

def estimate(filepath):
    """Bir dosyanın kaç token tutacağını tahmin et"""
    root = find_root()
    full = root / filepath
    if not full.exists():
        full = Path(filepath)
    if not full.exists():
        print(json.dumps({"error": f"Dosya bulunamadı: {filepath}"}))
        return

    content = full.read_text(encoding="utf-8-sig", errors="replace")
    lines = content.splitlines()
    token_est = int(len(content.split()) * 1.3)

    data = load_budget()
    remaining = data["max_tokens"] - data["used_tokens"]
    fits = token_est <= remaining

    print(json.dumps({
        "file": filepath,
        "lines": len(lines),
        "estimated_tokens": token_est,
        "remaining_budget": remaining,
        "fits_in_budget": fits,
        "recommendation": (
            "Tüm dosyayı gönderebilirsin." if fits and token_est < 500
            else "get_symbol ile sadece ihtiyacın olan fonksiyonu al." if token_est > 500
            else "Dikkatli ol, bütçe azalıyor." if not fits
            else "OK"
        )
    }, indent=2))

def reset():
    lock = budget_lock()
    if lock:
        with lock:
            data = load_budget()
            data["used_tokens"] = 0
            data["entries"] = []
            data["started_at"] = datetime.now().isoformat()
            save_budget(data)
    else:
        data = load_budget()
        data["used_tokens"] = 0
        data["entries"] = []
        data["started_at"] = datetime.now().isoformat()
        save_budget(data)
    print(json.dumps({"status": "reset", "scope": scope_key(), "max_tokens": data["max_tokens"]}))

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", type=int, metavar="MAX_TOKENS")
    group.add_argument("--spend", nargs="+", metavar=("TOKENS", "REASON"))
    group.add_argument("--status", action="store_true")
    group.add_argument("--estimate", metavar="FILE")
    group.add_argument("--reset", action="store_true")
    group.add_argument("--model", metavar="MODEL_NAME", help="Model seç")
    args = parser.parse_args()

    if args.init:
        init_budget(args.init)
    elif args.spend:
        tokens = int(args.spend[0])
        reason = " ".join(args.spend[1:]) if len(args.spend) > 1 else ""
        spend(tokens, reason)
    elif args.status:
        status()
    elif args.estimate:
        estimate(args.estimate)
    elif args.reset:
        reset()
    elif args.model:
        data = load_budget()
        data["model"] = args.model
        save_budget(data)
        print(json.dumps({"model_set": args.model}))

if __name__ == "__main__":
    main()
