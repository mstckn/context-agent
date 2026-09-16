#!/usr/bin/env python3
"""
directives.py — task-aware playbook/directive layer for Context Agent.

Bu modül, ``build_context`` paketine "doğru kod" yanında "doğru yöntem"i de
ekler. ``.context/directives/*.md`` altında insan (veya güçlü bir model
tarafından offline) yazılmış playbook'lar durur; her playbook'un frontmatter'ı
hangi görevlerde geçerli olduğunu söyler (op_type / domains / keywords /
dosya glob'ları). Bir görev geldiğinde, tıpkı context routing gibi, direktifler
de skorlanıp token-bütçesine sığdırılarak retrieve edilir.

Neden ayrı katman:
- **Harness-bağımsız:** Hem Claude Code hem Codex/diğer MCP client'lar aynı
  direktifi alır (skill'in yapamadığı şey).
- **Runtime'da güçlü-model çağrısı gerekmez:** Direktif kütüphanesi offline
  yazılır; çalışma anında sadece retrieve edilir.
- **Tazelik/dedup bedava:** Mevcut altyapıyla aynı paketin parçası olur.

Frontmatter formatı (stdlib-only parse, PyYAML gerekmez)::

    ---
    id: frontend-screens
    title: Frontend ekran & WebSocket kuralları
    match_op: feature, modify, bugfix      # op_type eşleşmesi
    match_domains: api                       # route domain eşleşmesi
    match_keywords: screen, component, tsx   # görev kelimeleri
    match_globs: src/**/*.tsx, src/lib/ws-base.ts  # routed dosya eşleşmesi
    always: false                            # true ise her görevde dahil
    priority: 20                             # eşit skorda sıralama
    budget_tokens: 450                       # bu direktifin tahmini maliyeti
    ---
    - Madde 1 ...
    - Madde 2 ...

CLI::

    python directives.py --list
    python directives.py --task "fix the login websocket" --budget 1200
    python directives.py --task "..." --op-type feature --domains api,db \\
        --files src/a.tsx,src/b.ts --budget 1200
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Skor ağırlıkları — context routing'in skorlama felsefesiyle uyumlu:
# routed dosya eşleşmesi en güçlü sinyal (direktif fiilen sahnedeki dosyalar
# hakkında), onu op_type ve domain takip eder, keyword en zayıf sinyal.
WEIGHT_OP = 30
WEIGHT_DOMAIN = 15
WEIGHT_KEYWORD = 8
WEIGHT_GLOB = 25
KEYWORD_MATCH_CAP = 4          # bir direktif için keyword puanı bu kadarda doyar
ALWAYS_BASE_SCORE = 1000       # "always" direktifler her zaman önde
DEFAULT_DIRECTIVE_BUDGET = 1200
DEFAULT_MAX_ITEMS = 6

_LIST_SPLIT = re.compile(r"[,\n]")


def _estimate_tokens(text):
    """token_count varsa onu kullan, yoksa dile-duyarlı kaba tahmin."""
    try:
        import token_count  # type: ignore
        return token_count.count_tokens(text or "")
    except Exception:
        return int(len(str(text or "").split()) * 1.3)


def _split_list(value):
    if value is None:
        return []
    return [item.strip() for item in _LIST_SPLIT.split(str(value)) if item.strip()]


def _coerce_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on", "evet")


def directives_dir(root):
    return Path(root) / ".context" / "directives"


def parse_directive(path):
    """Tek bir .md playbook dosyasını {meta, body} olarak ayrıştır."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    meta = {}
    body = raw

    if raw.lstrip().startswith("---"):
        # İlk '---' bloğunu frontmatter olarak al.
        stripped = raw.lstrip()
        rest = stripped[3:]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
            body = rest[end + 4:].lstrip("\n")
            for line in front.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                meta[key.strip().lower()] = val.strip()

    directive_id = meta.get("id") or Path(path).stem
    return {
        "id": directive_id,
        "title": meta.get("title", directive_id),
        "match_op": [m.lower() for m in _split_list(meta.get("match_op"))],
        "match_domains": [m.lower() for m in _split_list(meta.get("match_domains"))],
        "match_keywords": [m.lower() for m in _split_list(meta.get("match_keywords"))],
        "match_globs": [g.replace("\\", "/") for g in _split_list(meta.get("match_globs"))],
        "always": _coerce_bool(meta.get("always", "false")),
        "priority": int(re.sub(r"[^0-9-]", "", meta.get("priority", "0")) or 0),
        "budget_tokens": int(re.sub(r"[^0-9]", "", meta.get("budget_tokens", "0")) or 0),
        "body": body.strip(),
        "source": Path(path).name,
    }


def load_directives(root):
    """`.context/directives/*.md` altındaki tüm playbook'ları yükle."""
    base = directives_dir(root)
    if not base.exists():
        return []
    out = []
    for path in sorted(base.glob("*.md")):
        if path.name.lower() in ("readme.md",):
            continue
        try:
            out.append(parse_directive(path))
        except Exception as exc:  # tek bozuk dosya tüm paketi düşürmesin
            out.append({"id": path.stem, "error": str(exc), "source": path.name})
    return out


def _glob_match(path, pattern):
    # fnmatch '*' karakteri '/' üzerinden de eşleşir; '**/' kalıplarını sadeleştir.
    norm = (path or "").replace("\\", "/")
    pat = pattern.replace("**/", "").replace("**", "*")
    return fnmatch.fnmatch(norm, pat)


def score_directive(directive, op_type, domains, keywords, files):
    """Bir direktifi göreve göre skorla; eşleşen sinyalleri de raporla."""
    if directive.get("error"):
        return 0, {}

    op_type = (op_type or "").lower()
    domains = {d.lower() for d in (domains or [])}
    kw_set = {k.lower() for k in (keywords or [])}
    files = files or []

    score = 0
    matched = {"op": False, "domains": [], "keywords": [], "globs": []}

    if directive["match_op"] and op_type and op_type in directive["match_op"]:
        score += WEIGHT_OP
        matched["op"] = True

    for dom in directive["match_domains"]:
        if dom in domains:
            score += WEIGHT_DOMAIN
            matched["domains"].append(dom)

    kw_hits = 0
    for kw in directive["match_keywords"]:
        if kw in kw_set or any(kw in str(k) for k in kw_set):
            kw_hits += 1
            matched["keywords"].append(kw)
    score += WEIGHT_KEYWORD * min(kw_hits, KEYWORD_MATCH_CAP)

    for pattern in directive["match_globs"]:
        hit = next((f for f in files if _glob_match(f, pattern)), None)
        if hit:
            score += WEIGHT_GLOB
            matched["globs"].append(pattern)

    if directive["always"]:
        score += ALWAYS_BASE_SCORE
        matched["always"] = True

    return score, matched


def select_directives(
    root,
    op_type=None,
    domains=None,
    keywords=None,
    files=None,
    budget_tokens=DEFAULT_DIRECTIVE_BUDGET,
    max_items=DEFAULT_MAX_ITEMS,
):
    """Göreve uygun direktifleri skorla, bütçeye sığdırarak seç.

    Döner::

        {
          "items": [ {id, title, score, matched, body, token_estimate, source}, ... ],
          "token_estimate": int,
          "considered": int,
          "matched": int,
          "budget_tokens": int,
          "directives_dir_exists": bool
        }
    """
    all_directives = load_directives(root)
    scored = []
    for directive in all_directives:
        score, matched = score_directive(directive, op_type, domains, keywords, files)
        if score <= 0:
            continue
        tokens = directive["budget_tokens"] or _estimate_tokens(directive["body"])
        scored.append((score, directive, matched, tokens))

    # Önce skor, eşitlikte priority, sonra id (deterministik).
    scored.sort(key=lambda row: (-row[0], -row[1]["priority"], row[1]["id"]))

    selected = []
    used = 0
    for score, directive, matched, tokens in scored:
        if len(selected) >= max_items:
            break
        if used + tokens > budget_tokens and selected:
            # Bütçe doldu; "always" değilse atla. always olanlara yer aç.
            if not directive["always"]:
                continue
        used += tokens
        selected.append({
            "id": directive["id"],
            "title": directive["title"],
            "score": score,
            "matched": matched,
            "priority": directive["priority"],
            "body": directive["body"],
            "token_estimate": tokens,
            "source": directive["source"],
        })

    return {
        "items": selected,
        "token_estimate": used,
        "considered": len(all_directives),
        "matched": len(scored),
        "budget_tokens": budget_tokens,
        "directives_dir_exists": directives_dir(root).exists(),
    }


def render_directives_markdown(selection):
    """Seçili direktifleri system_prompt'a gömülecek markdown'a çevir."""
    items = selection.get("items", [])
    if not items:
        return ""
    lines = ["## Proje Direktifleri (Playbook)"]
    for item in items:
        lines.append(f"\n### {item['title']}  ·  [{item['id']}]")
        lines.append(item["body"])
    return "\n".join(lines).strip()


def stable_directives(root):
    """`always: true` (proje-global) direktifleri döndür — cache'lenebilir kanal için."""
    return [d for d in load_directives(root) if d.get("always") and not d.get("error")]


def stable_directives_markdown(root):
    """Always-direktifleri kalıcı `rules` resource'una gömmek için markdown."""
    items = stable_directives(root)
    if not items:
        return ""
    lines = ["## Proje Geneli Direktifler (sabit — oturumda bir kez okunması yeterli)"]
    for d in items:
        lines.append(f"\n### {d['title']}  ·  [{d['id']}]")
        lines.append(d["body"])
    return "\n".join(lines).strip()


# ── CLI ──────────────────────────────────────────────────────────────────────

def _find_root():
    try:
        from paths import find_project_root
        return find_project_root()
    except Exception:
        # .context yukarı doğru ara
        current = Path.cwd().resolve()
        for parent in [current] + list(current.parents):
            if (parent / ".context").exists():
                return parent
        return current


def _route_analysis(root, task, budget):
    """route.py'yi çağırıp analysis + routed dosya yollarını çıkar (CLI --task modu)."""
    script = Path(root) / ".context" / "scripts" / "route.py"
    if not script.exists():
        return {}, []
    try:
        result = subprocess.run(
            [sys.executable, str(script), task, "--budget", str(budget)],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except Exception:
        return {}, []
    analysis = data.get("analysis", {})
    files = [f.get("file") for f in data.get("relevant_files", []) if f.get("file")]
    return analysis, files


def main():
    parser = argparse.ArgumentParser(description="Context Agent directive/playbook layer")
    parser.add_argument("--task", help="Görev metni; route üzerinden analiz çıkarılır.")
    parser.add_argument("--op-type", help="op_type override (route'u atla).")
    parser.add_argument("--domains", help="Virgülle ayrılmış domain listesi.")
    parser.add_argument("--keywords", help="Virgülle ayrılmış keyword listesi.")
    parser.add_argument("--files", help="Virgülle ayrılmış routed dosya yolları.")
    parser.add_argument("--budget", type=int, default=DEFAULT_DIRECTIVE_BUDGET)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--list", action="store_true", help="Tüm direktifleri özetle.")
    parser.add_argument("--stable", action="store_true",
                        help="Sadece always-direktifleri markdown olarak yazdır (rules resource için).")
    parser.add_argument("--root", help="Proje kökü (vermezsen otomatik bulunur).")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else _find_root()

    if args.stable:
        sys.stdout.write(stable_directives_markdown(root))
        return

    if args.list:
        directives = load_directives(root)
        summary = [
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "match_op": d.get("match_op"),
                "match_domains": d.get("match_domains"),
                "match_keywords": d.get("match_keywords"),
                "match_globs": d.get("match_globs"),
                "always": d.get("always"),
                "priority": d.get("priority"),
                "source": d.get("source"),
                "error": d.get("error"),
            }
            for d in directives
        ]
        print(json.dumps({
            "directives_dir": str(directives_dir(root)),
            "directives_dir_exists": directives_dir(root).exists(),
            "count": len(directives),
            "directives": summary,
        }, ensure_ascii=False, indent=2))
        return

    op_type = args.op_type
    domains = _split_list(args.domains)
    keywords = _split_list(args.keywords)
    files = _split_list(args.files)

    if args.task and not (op_type or domains or keywords or files):
        analysis, routed_files = _route_analysis(root, args.task, args.budget)
        op_type = analysis.get("op_type")
        domains = analysis.get("domains", [])
        keywords = analysis.get("keywords", [])
        files = routed_files

    selection = select_directives(
        root,
        op_type=op_type,
        domains=domains,
        keywords=keywords,
        files=files,
        budget_tokens=args.budget,
        max_items=args.max_items,
    )
    selection["inputs"] = {
        "op_type": op_type,
        "domains": domains,
        "keywords": keywords[:20] if keywords else [],
        "files": files,
    }
    print(json.dumps(selection, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
