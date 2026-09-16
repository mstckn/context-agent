#!/usr/bin/env python3
"""
install.py — Context Agent'ı projeye kurar.

Kullanım:
  python install.py                    # mevcut dizine kur
  python install.py /path/to/project   # belirtilen dizine kur
"""

import sys, shutil, json, hashlib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = [
    "install.py",
    "index.py",
    "get_symbol.py",
    "search.py",
    "get_range.py",
    "get_related.py",
    "safe_io.py",
    "token_count.py",
    "usage.py",
    "compress_log.py",
    "route.py",
    "dedup.py",
    "budget.py",
    "session.py",
    "capsule.py",
    "directives.py",
    "agent.py",
    "mcp_server.py",
    "mcp_launcher.py",
    "dashboard_server.py",
    "from_log.py",
    "eval.py",
    "eval_answer_quality.py",
    "benchmark_token_savings.py",
    "watch.py",
    "ide_detector.py",
    "global_install.py",
    "mcp_server_http.py",
    "lifecycle_manager.py",
    "git_meta.py",
    "paths.py",
    "config_store.py",
    "content_index.py",
    "graph.py",
    "identity.py",
    "ledger.py",
    "memory.py",
    "model_gateway.py",
    "model_profile.py",
    "planner.py",
    "router.py",
    "router_mcp_server.py",
    "one_shot.py",
    "secret_store.py",
    "sufficiency.py",
]

AIIGNORE_DEFAULT = """\
# Context Agent — .aiignore
# Bu dosyadaki pattern'ler indexlenmez

# Context Agent çıktıları
.context/

# Paket yöneticileri
node_modules/
vendor/
.venv/
venv/
env/
__pycache__/

# Build çıktıları
dist/
build/
out/
.next/
target/
*.class
*.pyc

# Binary & medya
*.png
*.jpg
*.jpeg
*.gif
*.ico
*.pdf
*.zip
*.tar.gz
*.woff
*.woff2

# Üretilmiş kod
# NOT: migrations/ bilinçli olarak ENGELLENMEZ; şema/contract bilgisi
# DATABASE_CHANGE görevleri için kritiktir (index.py DEFAULT_IGNORE ile aynı).
*.generated.*
*_pb2.py
*.g.dart

# Gizli bilgiler
.env
.env.*
*.key
*.pem
secrets/

# Lock dosyaları
package-lock.json
yarn.lock
Cargo.lock
poetry.lock

# Test snapshot'ları
__snapshots__/
*.snap

# Log dosyaları
*.log
logs/
"""

SYSTEM_PROMPT = """\
# Context Agent System Prompt

Sen bir coding assistant'sın. Çalıştığın proje hakkında şunları biliyorsun:

## İlk Adım
`.context/map.json` dosyasını oku. Bu dosya projenin tüm anatomisini içerir:
- Modüller ve dosya özetleri
- Sembol indexi (fonksiyon, class, interface isimleri ve konumları)
- Bağımlılık ilişkileri

## Kurallar
1. Bir dosyayı düzenlemeden önce MUTLAKA önce oku
2. Sembol adını biliyorsan: `get_symbol` kullan
3. Ne aradığını bilmiyorsan: `search` kullan
4. Dosyanın bağımlılıklarını anlamak için: `get_related` kullan
5. Hata logu varsa önce sıkıştır: `compress_log`
6. Yeni task başlarken: `route` ile hangi dosyaların gerekli olduğunu öğren

## Script'ler
```
python .context/scripts/get_symbol.py <SymbolAdı>
python .context/scripts/search.py "<sorgu>"
python .context/scripts/get_range.py <dosya> [başlangıç] [bitiş]
python .context/scripts/get_related.py <dosya>
echo '<log>' | python .context/scripts/compress_log.py
python .context/scripts/route.py "<task açıklaması>"
python .context/scripts/index.py --file <dosya>   # dosya değiştikten sonra
```

## Token Bütçesi
- map.json oku: ~1-2k token (bir kez)
- get_symbol çağrısı: ~200-500 token
- Hedef: Toplam context < 8k token

## Önemli
- Var olmayan sembol adı tahmin etme — önce search ile bul
- Dosyayı değiştirdikten sonra index güncelle: `index.py --file <dosya>`

## Kaçış yolu — motor zorunlu değil
Context Agent bir optimizasyon, kapı değil. Şu durumlarda baypas et, dosyayı doğrudan oku:
- Araç hata verir / indeks yok / status sağlıksız.
- `sufficiency.sufficient=false` veya güven `low` → önerilen dosyaları doğrudan oku.
- Item `partial: true` (iskelet/pencere) ve gövde lazım → `full_path`'i oku veya `expand_commands`.
- Tam sadakat için: `build_context(raw=true)`.
Asla motoru beklerken tıkanma — taban her zaman "doğrudan oku".
"""

DIRECTIVES_README = """\
# Proje Direktifleri (Playbook Katmanı)

Bu klasördeki her `.md` dosyası bir **playbook**'tur: belirli görev tiplerinde
LLM'in *nasıl* çalışması gerektiğini anlatan, önceden (insan veya güçlü bir
model tarafından offline) yazılmış rehberlik. Context engine, bir görev geldiğinde
ilgili dosyaları (context) getirirken bu direktifleri de skorlayıp pakete ekler.

## Neden
- **Harness-bağımsız:** Claude Code, Codex ve diğer MCP client'ların hepsi aynı
  direktifi alır — skill'in yapamadığı şey.
- **Bedava runtime:** Direktif kütüphanesini bir kez yazarsın; çalışma anında
  güçlü-model çağrısı gerekmez, sadece retrieve edilir.

## Frontmatter alanları
```
---
id: benzersiz-id
title: İnsan-okur başlık
match_op: feature, modify, bugfix    # op_type eşleşmesi (bugfix/feature/refactor/modify/test)
match_domains: api, db               # route domain eşleşmesi (auth/api/db/user/...)
match_keywords: websocket, ws, chat  # görev metnindeki kelimeler
match_globs: src/**/*.tsx            # routed dosya yolu eşleşmesi (en güçlü sinyal)
always: false                        # true ise HER görevde dahil (proje-global kural)
priority: 20                         # eşit skorda sıralama
budget_tokens: 400                   # tahmini token maliyeti (boşsa gövdeden hesaplanır)
---
- Madde madde, kısa ve emir kipinde direktifler.
```

## İpuçları
- En güçlü eşleşme sinyali `match_globs` — direktif fiilen sahnedeki dosyalar
  hakkındaysa kullan.
- Proje geneli kurallar için `always: true` kullan ama kısa tut (her görevde
  bütçe yer).
- Direktifleri **eval/benchmark görevlerine bakarak yazma** — genellemeyi bozar.
- Bu klasörü versiyon kontrolüne almak istersen `.aiignore` / `.gitignore`'da
  `.context/directives/` için bir istisna ekleyebilirsin.

Eşleşmeyi denemek için: `python .context/scripts/directives.py --task "..."`
"""

DIRECTIVE_EXAMPLE = """\
---
id: example-project-conventions
title: Örnek — Proje konvansiyonları
always: true
priority: 5
budget_tokens: 120
---
- Bu bir örnek direktiftir; kendi proje kurallarınla değiştir veya sil.
- Bir dosyayı değiştirdikten sonra `index.py --file <dosya>` ile tazele.
- Mevcut yardımcıları/servisleri yeniden kullan; paralel implementasyon yazma.
"""


def scaffold_directives(context_dir):
    """`.context/directives/` klasörünü README + örnekle oluştur (varsa ezme)."""
    directives_dir = context_dir / "directives"
    created = not directives_dir.exists()
    directives_dir.mkdir(exist_ok=True)

    readme = directives_dir / "README.md"
    if not readme.exists():
        readme.write_text(DIRECTIVES_README, encoding="utf-8")

    example = directives_dir / "example-project-conventions.md"
    # Örneği yalnızca klasör yeni oluşturulduysa yaz — kullanıcı silmişse geri gelmesin.
    if created and not example.exists():
        example.write_text(DIRECTIVE_EXAMPLE, encoding="utf-8")

    return created


def append_unique_line(path, line):
    """Dosyaya aynı ignore satırını tekrar eklemeden yaz."""
    if path.exists():
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = {existing.strip() for existing in content.splitlines()}
        if line.strip() in lines:
            return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{line}\n")
        return True

    path.write_text(f"{line}\n", encoding="utf-8")
    return True

def install(target_dir=None):
    if target_dir:
        root = Path(target_dir).resolve()
    else:
        root = Path.cwd()

    print(f"[context-agent] Kuruluyor: {root}")

    # .context dizini oluştur
    context_dir = root / ".context"
    scripts_dir = context_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "summaries").mkdir(exist_ok=True)
    (context_dir / "sessions").mkdir(exist_ok=True)
    (context_dir / "evals").mkdir(exist_ok=True)

    try:
        from paths import ensure_project_id

        project_id = ensure_project_id(root)
        print(f"  [ok] project_id hazır: {project_id[:12]}")
    except Exception as e:
        print(f"  [warn] project_id yazılamadı: {e}")

    # Direktif/playbook klasörünü scaffold et (mevcut direktifleri ezmeden)
    if scaffold_directives(context_dir):
        print("  [ok] .context/directives/ oluşturuldu (README + örnek)")
    else:
        print("  [skip] .context/directives/ zaten var")

    # Script'leri kopyala
    source_dir = Path(__file__).parent
    copied = 0
    for script in SCRIPTS:
        src = source_dir / script
        dst = scripts_dir / script
        if src.exists():
            if src.resolve() == dst.resolve():
                print(f"  [skip] {script} (zaten kurulu)")
                continue
            shutil.copy2(src, dst)
            copied += 1
            print(f"  [ok] {script}")
        else:
            print(f"  [missing] {script} bulunamadı: {src}")

    # Eval fixtures
    try:
        evals_src = source_dir.parent / "evals"
        evals_dst = context_dir / "evals"
        if evals_src.exists():
            for fixture in sorted(evals_src.glob("*.json")):
                dst_fixture = evals_dst / fixture.name
                if fixture.resolve() == dst_fixture.resolve():
                    continue
                shutil.copy2(fixture, dst_fixture)
            print("  [ok] eval fixtures kopyalandı (.context/evals)")
        else:
            print(f"  [skip] evals klasörü yok: {evals_src}")
    except Exception as e:
        print(f"  [warn] eval fixtures kopyalanamadı: {e}")

    # Install meta: launcher can detect when .context scripts are stale
    try:
        h = hashlib.sha256()
        for script in SCRIPTS:
            p = source_dir / script
            if p.exists():
                h.update(p.read_bytes())
        meta = {"version": 1, "scripts_sha256": h.hexdigest()}
        (context_dir / "install_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("  [ok] install_meta.json yazıldı")
    except Exception as e:
        print(f"  [warn] install_meta.json yazılamadı: {e}")

    # .aiignore oluştur
    aiignore_path = root / ".aiignore"
    if not aiignore_path.exists():
        aiignore_path.write_text(AIIGNORE_DEFAULT, encoding="utf-8")
        print("  [ok] .aiignore oluşturuldu")
    else:
        if append_unique_line(aiignore_path, ".context/"):
            print("  [ok] .aiignore güncellendi (.context/)")
        else:
            print("  [skip] .aiignore zaten hazır")

    # system_prompt.md yaz
    prompt_path = context_dir / "system_prompt.md"
    prompt_path.write_text(SYSTEM_PROMPT, encoding="utf-8")
    print("  [ok] system_prompt.md oluşturuldu")

    # .gitignore'a .context ekle (varsa)
    gitignore = root / ".gitignore"
    if append_unique_line(gitignore, ".context/"):
        print("  [ok] .gitignore güncellendi")
    else:
        print("  [skip] .gitignore zaten hazır")

    print(f"\n[ok] {copied} script kuruldu")
    print("\nSonraki adım - indexlemeyi başlat:")
    print(f"   cd {root}")
    print("   python .context/scripts/index.py")
    print("\nSystem prompt: .context/system_prompt.md")
    print("Proje haritası: .context/map.json (indexlemeden sonra)")

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    install(target)

if __name__ == "__main__":
    main()
