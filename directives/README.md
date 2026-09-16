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
