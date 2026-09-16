#!/usr/bin/env python3
"""
compress_log.py — Terminal/hata loglarını sıkıştırır.

Kullanım:
  cat error.log | python compress_log.py
  python compress_log.py --file error.log
  python compress_log.py --file error.log --mode summary
"""

import sys, json, re, argparse
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Bizim kodumuzla ilgili path pattern'leri
OUR_CODE_PATTERNS = [
    r'File "(?!.*/site-packages/)(?!.*/dist-packages/)',
    r'at (?!Object\.|Array\.|Function\.)[\w.]+\s*\(',
    r'src/',
    r'app/',
    r'lib/',
]

ERROR_PATTERNS = [
    r'(Error|Exception|FATAL|CRITICAL|Panic|panic)',
    r'(Traceback|traceback)',
    r'(WARN|WARNING)',
    r'^\s*\^+\s*$',  # Python hata göstergesi
]

NOISE_PATTERNS = [
    r'node_modules/',
    r'site-packages/',
    r'dist-packages/',
    r'/usr/lib/',
    r'<anonymous>',
    r'processTicksAndRejections',
    r'Module\._compile',
]

def compress_log(text, mode="smart"):
    lines = text.strip().split('\n')
    total_lines = len(lines)
    total_tokens = int(len(text.split()) * 1.3)

    if mode == "full":
        print(json.dumps({"content": text, "mode": "full"}))
        return

    kept = []
    error_blocks = []
    current_block = None

    for i, line in enumerate(lines):
        is_noise = any(re.search(p, line) for p in NOISE_PATTERNS)
        is_error = any(re.search(p, line) for p in ERROR_PATTERNS)
        is_our_code = any(re.search(p, line) for p in OUR_CODE_PATTERNS)
        is_last = i >= total_lines - 5

        if is_error and not is_noise:
            if current_block is None:
                current_block = {"start": i, "lines": []}
            current_block["lines"].append({"n": i+1, "text": line.strip(), "tag": "error"})
        elif is_our_code and not is_noise:
            if current_block:
                current_block["lines"].append({"n": i+1, "text": line.strip(), "tag": "our_code"})
            else:
                kept.append({"n": i+1, "text": line.strip(), "tag": "our_code"})
        elif is_last and line.strip():
            kept.append({"n": i+1, "text": line.strip(), "tag": "final"})
        else:
            if current_block and not is_noise:
                # Blok kapandı, kaydet
                error_blocks.append(current_block)
                current_block = None

    if current_block:
        error_blocks.append(current_block)

    all_kept = []
    for block in error_blocks:
        all_kept.extend(block["lines"])
    all_kept.extend(kept)

    # Tekrarları kaldır
    seen = set()
    unique_kept = []
    for item in all_kept:
        key = item["text"][:80]
        if key not in seen:
            seen.add(key)
            unique_kept.append(item)

    unique_kept.sort(key=lambda x: x["n"])

    compressed_text = "\n".join(f"[{x['n']}] {x['text']}" for x in unique_kept)
    compressed_tokens = int(len(compressed_text.split()) * 1.3)

    savings_pct = int((1 - compressed_tokens / max(total_tokens, 1)) * 100)

    print(json.dumps({
        "compressed": compressed_text,
        "kept_lines": len(unique_kept),
        "total_lines": total_lines,
        "original_tokens": total_tokens,
        "compressed_tokens": compressed_tokens,
        "savings_percent": savings_pct
    }, indent=2, ensure_ascii=False))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Log dosyası (yoksa stdin)")
    parser.add_argument("--mode", default="smart", choices=["smart", "full", "summary"])
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8-sig", errors="replace")
    else:
        text = sys.stdin.read()

    compress_log(text, args.mode)

if __name__ == "__main__":
    main()
