#!/usr/bin/env python3
"""
get_range.py — Dosyanın belirli satır aralığını getirir.

Kullanım:
  python get_range.py src/auth/service.py 45 89
  python get_range.py src/auth/service.py 45       # 45'ten dosya sonuna
  python get_range.py src/auth/service.py           # tüm dosya (özet modda)
"""

import sys, json, argparse
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def find_root():
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".context").exists():
            return parent
    return Path.cwd()

def _chunk_map(filepath, root, total_lines, preview_end=50):
    """Büyük dosyanın geri kalanını sembol/satır haritası olarak gösterir.

    Böylece önizleme modunda dosyanın kalanına kör kalınmaz: model hangi
    aralığı isteyeceğini haritadan seçer (spec: büyük dosya düzeltmesi).
    """
    rel = str(filepath).replace("\\", "/")
    entries = []
    try:
        import sqlite3
        db = Path(root) / ".context" / "symbols.db"
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=5)
            try:
                rows = conn.execute(
                    "SELECT name, type, start_line, end_line FROM symbols "
                    "WHERE file=? AND start_line > ? ORDER BY start_line",
                    (rel, preview_end)).fetchall()
            finally:
                conn.close()
            for name, typ, s, e in rows[:20]:
                if not s:
                    continue
                e = e or total_lines
                entries.append({
                    "name": name,
                    "type": typ or "",
                    "lines": f"{s}-{e}",
                    "get_cmd": f"python .context/scripts/get_range.py {rel} {s} {e}",
                })
    except Exception:
        entries = []
    if not entries:
        # Sembol bilgisi yoksa kaba satır haritası.
        step = 150
        for s in range(preview_end + 1, total_lines + 1, step):
            e = min(s + step - 1, total_lines)
            entries.append({
                "name": f"lines {s}-{e}",
                "type": "range",
                "lines": f"{s}-{e}",
                "get_cmd": f"python .context/scripts/get_range.py {rel} {s} {e}",
            })
    return entries

def get_range(filepath, start=None, end=None):
    root = find_root().resolve()
    try:
        full_path = (root / filepath).resolve()
        full_path.relative_to(root)
    except (OSError, ValueError):
        print(json.dumps({"error": "path_outside_project", "file": str(filepath)}))
        return
    if not full_path.exists() or not full_path.is_file():
        print(json.dumps({"error": f"Dosya bulunamadı: {filepath}"}))
        return

    content = full_path.read_text(encoding="utf-8-sig", errors="replace")
    lines = content.splitlines()
    total_lines = len(lines)

    if start is None:
        # Tüm dosya — çok büyükse özetle
        if total_lines > 200:
            preview = "\n".join(lines[:50])
            print(json.dumps({
                "file": filepath,
                "total_lines": total_lines,
                "mode": "preview",
                "note": f"Dosya büyük ({total_lines} satır). İlk 50 satır gösteriliyor; kalan chunk_map'ten seçilir.",
                "content": preview,
                "chunk_map": _chunk_map(filepath, root, total_lines),
                "get_full": f"python .context/scripts/get_range.py {filepath} 1 {total_lines}",
                "token_estimate": int(len(preview.split()) * 1.3)
            }, indent=2))
        else:
            print(json.dumps({
                "file": filepath,
                "total_lines": total_lines,
                "content": content,
                "token_estimate": int(len(content.split()) * 1.3)
            }, indent=2))
        return

    start = max(1, start)
    end = min(end or total_lines, total_lines)
    selected = "\n".join(lines[start-1:end])

    print(json.dumps({
        "file": filepath,
        "lines": f"{start}-{end}",
        "total_lines": total_lines,
        "content": selected,
        "token_estimate": int(len(selected.split()) * 1.3)
    }, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Dosya yolu")
    parser.add_argument("start", nargs="?", type=int, help="Başlangıç satırı")
    parser.add_argument("end", nargs="?", type=int, help="Bitiş satırı")
    args = parser.parse_args()
    get_range(args.file, args.start, args.end)

if __name__ == "__main__":
    main()
