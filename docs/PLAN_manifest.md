# Plano: Sistema de Manifest (port do jxl-photo)

Status: **implementado** (v2.2) — ver `README.md` seção "Manifest System" e
`tests/test_manifest.py`. Este documento é o plano original, mantido para
referência histórica. Referência: `jxl-photo/jxl_photo.py`
(funções `_load_manifest_entries`, `_execute_manifest_workflow`,
`_generate_manifest`, `_manifest_output_collisions`, `_manifest_source_overlaps`)
e seus testes (`tests/test_audit_priority2.py`,
`tests/test_throughput_and_manifest_encoding.py`, `tests/test_audit_delete_gates.py`).

---

## 1. Objetivo e escopo

Permitir gerar, editar (Excel) e executar um CSV de manifesto com uma linha por
pasta, cada linha com seu próprio modo — igual ao jxl-photo, mas com escopo
menor porque aqui existe **um único backend modeful**: `compress_tiff_zip.ps1`
(modos 0-9). Não há coluna `Direction` nem roteamento entre scripts.

**Dentro do escopo (MVP):**
- Manifesto apenas para o workflow 1 (Compress TIFFs, modos 0-9).
- Gerar manifesto a partir de uma pasta raiz (1 entry por subpasta com TIFFs).
- Rodar manifesto do menu (pick → validar → preview → confirmar → executar).
- Repetir último manifesto (integrado ao "Repeat last workflow" existente).
- Todos os guards de segurança portados do jxl-photo (seção 5).
- Sumário final por entry + log combinado.

**Fora do escopo (MVP):**
- Manifestos para workflows 2-4 (EXIF), 5-8 (restore/purge/diagnose/thumbnails).
- Presets nomeados / modo unattended (não existem neste repo).
- Auto Mode / análise de pasta com recomendação de modo por entry.
- `--summary-json` no backend: o jxl-photo agrega contagens por arquivo via JSON
  emitido pelos filhos. Aqui o MVP agrega só por **entry** (ok/falha via exit
  code 0/1 do `.ps1`, que já é confiável: `compress_tiff_zip.ps1` sai 1 quando
  `errTotal > 0`). Agregação por arquivo fica como melhoria futura.

## 2. Formato do CSV

```csv
Source,Destination,Mode
F:\2025\Tokyo\TIFF,F:\2025\Tokyo\ZIP_flat,2
F:\2025\Kyoto\TIFF,F:\2025\Kyoto\TIFF,0
# F:\2025\Osaka\TIFF,F:\2025\Osaka\TIFF,3
```

- Escrito com **UTF-8 BOM** (`utf-8-sig`) para o Excel detectar a codificação.
- Linhas começando com `#` são comentários (puladas).
- `Destination` vazio → cai para o valor de `Source` (nunca `""`, que vira `.`).
- **Destination só é honrado no modo 2** (`-OutputDir`). Nos demais modos o
  backend calcula o destino sozinho; o wrapper avisa quando a coluna for
  ignorada (regra análoga ao jxl-photo, onde só 0/2 honram).
- `Mode` vazio → usa o modo default perguntado no início do run (default =
  `cfg.config.last_mode`). `"7.0"` (Excel) → 7. `"7.5"` ou texto → recusa o
  manifesto inteiro. Faixa válida: **0-9**.
- Caminho com `..` em qualquer parte → recusa o manifesto inteiro.

## 3. Arquitetura (novas funções em `convert_tiff.py`)

`convert_tiff.py` é procedural (não há `MenuSystem` como no jxl-photo), então as
funções são module-level, seguindo o estilo do arquivo:

### Leitura / validação
- `_is_manifest_header_row(row) -> bool` — port direto (jxl_photo.py:294).
- `_open_manifest_for_read(path) -> Optional[StringIO]` — lê como `utf-8-sig`;
  retorna `None` em `UnicodeDecodeError` (ANSI re-salvo pelo Excel). **Nunca
  adivinhar encoding** (jxl_photo.py:1887).
- `load_manifest_entries(path) -> Optional[List[Tuple[str, str, Optional[int]]]]`
  — port de `_load_manifest_entries` (jxl_photo.py:2012), sem o direction guard.
  Falha fechada: qualquer problema retorna `None` com erro impresso.

### Geração / seleção / visualização
- `generate_manifest(root: Path, mode: int) -> Optional[str]` — varre `root`
  recursivamente, cria 1 entry por pasta que contém TIFFs (`.tif/.tiff`),
  escreve `manifests/manifest_YYYYMMDD_HHMMSS.csv` com BOM. Se só a raiz tem
  TIFFs, 1 entry.
- `get_latest_manifest() -> Optional[str]`, `pick_manifest() -> Optional[str]`
  — ports (jxl_photo.py:1909, 1919): lista `manifests/manifest_*.csv` por mtime,
  com 1 usa direto, com vários pergunta (máx. 10).
- `view_manifest(path) -> None` — tabela Rich (ou texto) das entries.
- `confirm_manifest_entries(path, entries) -> bool` — preview das 15 primeiras
  + confirmação (port de `_confirm_manifest_entries`, jxl_photo.py:2123).

### Guards pré-execução
- `manifest_source_overlaps(entries) -> List[Tuple[str, str]]` — detecta
  sources duplicados/aninhados (port de `_manifest_source_overlaps`,
  jxl_photo.py:3968). Run attended: aviso + "Run anyway? [y/N]" (default N).
- `manifest_output_collisions(entries) -> List[Tuple]` — só escaneia quando
  algum entry usa modo **2, 4 ou 5** (os únicos em que duas entries podem
  convergir: mesmo Destination flat, mesmo rename `_TIFF→_ZIP`, mesmo sibling
  `ZIP/`). Modos 0/1/3/6/7/8/9 escrevem dentro da própria árvore do Source —
  pula o scan e informa "Collision check: skipped (per-source output modes)".
  Colisão → recusa o run com a lista de conflitos.

### Execução
- `build_manifest_entry_cmd(entry, workflow, ps_name) -> List[str]` — monta o
  comando reutilizando `build_compress_command()` (convert_tiff.py:672) com um
  dict por entry: `mode` da linha, `folders=[Path(source)]`, `output_dir` só no
  modo 2, e os parâmetros globais do run (workers, staging, dry_run, thumbs).
- `execute_manifest_workflow(cfg, entries, workflow) -> bool` — port de
  `_execute_manifest_workflow` (jxl_photo.py:3381), simplificado:
  1. Aviso de Destination ignorado (modos != 2).
  2. Overlap check (warn + confirm).
  3. Collision check condicional (acima).
  4. Verificação de existência: qualquer Source inexistente → recusa o run
     (fail-closed; uma pasta "sumida" não pode parecer sucesso).
  5. **Gate do modo 8**: se qualquer entry tem `Mode == 8` e não é dry-run,
     exige a mesma confirmação vermelha do wizard (`run_free_compress`,
     convert_tiff.py:2112), **uma vez, antes de tudo rodar**. Sem isso,
     `-DeleteSource` nunca é passado.
  6. Loop: por entry imprime `[i/N] Mode M | src -> dst`, roda
     `run_subprocess(cmd)`, registra estado (`ok` rc=0 / `failed` rc!=0) e
     **continua** nas próximas entries.
  7. Sumário final: tabela por entry + totais + lista de entries falhadas
     (port simplificado de `_render_manifest_summary`, jxl_photo.py:3596 —
     sem contagens por arquivo no MVP).
  8. Log combinado em `Logs/convert_tiff/manifest_<timestamp>.log` contendo o
     bloco do sumário + caminhos dos logs filhos (o `.ps1` já grava o próprio
     log em `Logs/compress_tiff_zip/`).
  Retorna `True` só se zero falhas.
- `run_manifest_workflow(cfg) -> bool` — orquestra o fluxo do menu:
  `pick_manifest` → `load_manifest_entries` → pergunta modo default (para
  linhas sem Mode) + workers/staging/dry-run/thumbs (reusar `step_basic_params`)
  → `confirm_manifest_entries` → `execute_manifest_workflow` → salva
  `cfg.config.last_manifest_path` e `_save_last_run(...)`.

## 4. Integração com a UI

- **Menu principal** (`show_main_menu`, convert_tiff.py:1956): nova opção
  `"Run from manifest (CSV)"` entre "New workflow" e "Repeat last workflow".
  Indisponível se `manifests/` está vazio/ausente.
- **Geração**: no fim de `run_free_compress` (após escolher pasta+modo, antes de
  confirmar), oferecer `[P] Generate manifest CSV` como alternativa a rodar
  agora: chama `generate_manifest(folder, mode)`, mostra o caminho e volta.
  (Geração por subpastas cobre o caso de uso do jxl-photo sem Auto Mode.)
- **Repeat last workflow** (`run_repeat_last`, convert_tiff.py:2533): se o
  último run foi manifesto (`last_manifest_path` setado e o arquivo existe),
  o repeat re-lê o CSV com **o mesmo loader** (mesmos guards) e re-executa —
  nunca re-roda comandos cegos para manifesto. Manter o comportamento atual de
  `last_run_commands` para runs não-manifesto.
- **Config** (`ToolConfig`, convert_tiff.py:66): novo campo
  `last_manifest_path: Optional[str] = None` (persistido; guarda só o caminho).
- **.gitignore**: adicionar `manifests/` (igual ao jxl-photo).

## 5. Guards de segurança (checklist portado do jxl-photo)

| Guard | Comportamento | Origem |
|---|---|---|
| Encoding | Escreve BOM; lê `utf-8-sig`; recusa ANSI sem adivinhar | jxl_photo.py:1887 |
| Header | Só pula a 1ª linha se for mesmo header | jxl_photo.py:2038 |
| Mode Excel | `float()` → inteiro ok; fração/texto recusa tudo | jxl_photo.py:2058 |
| Faixa de modo | 0-9; fora disso recusa tudo | idem (0-8 lá) |
| Destination vazio | Fallback para Source | jxl_photo.py:2050 |
| Comentário `#` | Pulado; não vira entry | jxl_photo.py:2047 |
| Traversal `..` | Recusa o manifesto inteiro (não pula a linha) | jxl_photo.py:2079 |
| Sources sobrepostos | Aviso + confirmação (default: não) | jxl_photo.py:3438 |
| Colisão de output | Scan só modos 2/4/5; colisão → recusa | jxl_photo.py:3460 |
| Source inexistente | Recusa o run (fail-closed) | regra análoga |
| Modo 8 | Confirmação única antes do run; dry-run pula o gate | jxl_photo.py:3503 |
| Destination ignorado | Aviso listando até 5 entries (modos != 2) | jxl_photo.py:3398 |

Decisão consciente: manter a confirmação y/N do modo 8 (estilo atual deste
repo) em vez do HHMM do jxl-photo — consistência com `run_free_compress`.
Upgrade para HHMM pode vir depois, aplicado aos dois caminhos.

## 6. Testes (`tests/test_manifest.py`, pytest)

Seguir as convenções de `tests/test_convert_tiff.py` (import direto de
`convert_tiff`, `tmp_path`, `monkeypatch`, mocks de `run_subprocess` e dos
prompts Rich). Lista mínima, espelhando os testes do jxl-photo:

**Encoding / parsing** (espelha `test_throughput_and_manifest_encoding.py`):
1. Manifesto gerado começa com BOM (`b"\xef\xbb\xbf"`), inclusive com path
   não-ASCII (ex.: japonês).
2. Leitura remove BOM; manifesto com BOM é aceito.
3. Manifesto ANSI (bytes `cp1252` não-UTF-8) é **recusado**; nenhuma entry é
   construída.
4. Manifesto ASCII puro sem BOM funciona.
5. Header deletado: primeira entry não é perdida.
6. Comentários `#` e linhas vazias pulados; Destination vazio → Source.
7. `"7.0"` → 7; `"7.5"` recusado; `"abc"` recusado; `10` recusado (faixa 0-9).

**Guards** (espelha `test_audit_priority2.py` / `test_audit_delete_gates.py`):
8. Entry com `..` → manifesto inteiro recusado (não skip).
9. Overlaps: aninhado detectado, duplicado detectado, irmãos não detectam.
10. Colisões: duas entries modo 2, mesmo Destination, mesmo basename → recusa;
    nomes distintos → OK; manifesto só com modos 0/1/3/6/7/8/9 pula o scan.
11. Modo 8: o executor invoca a confirmação; recusar → nada roda
    (`run_subprocess` nunca chamado); aceitar → `-DeleteSource` presente só
    nas entries modo 8.
12. Source inexistente → run recusado.
13. Aviso de Destination ignorado aparece para modos != 2 com dest divergente.

**Execução**:
14. Um comando por entry, com `-Mode` da linha e `-InputDir` corretos; modo 2
    recebe `-OutputDir`.
15. Falha em uma entry (rc=1 mockado) não impede as seguintes; retorno final
    `False`; sumário conta ok/failed corretamente.
16. Dry-run propaga `-DryRun` e pula o gate do modo 8.
17. Linha sem Mode usa o modo default do run.
18. `last_manifest_path` é persistido; repeat re-lê via `load_manifest_entries`
    (mock provando que o loader é chamado no repeat).

**Geração**:
19. `generate_manifest` cria 1 entry por subpasta com TIFFs (criar `.tif`
    vazios em `tmp_path`), com o modo pedido e BOM.
20. Pasta sem TIFFs → retorna `None` com mensagem.

Rodar: `pytest tests/test_manifest.py -v` + suíte completa
(`pytest tests/ -k "not pester"`) para garantir que nada regrediu.

## 7. Ordem de implementação

1. `ToolConfig.last_manifest_path` + `.gitignore` + helpers de leitura
   (`_is_manifest_header_row`, `_open_manifest_for_read`, `load_manifest_entries`)
   + testes 1-8.
2. Geração/seleção/view (`generate_manifest`, `get_latest_manifest`,
   `pick_manifest`, `view_manifest`) + testes 19-20.
3. Guards de execução (`manifest_source_overlaps`,
   `manifest_output_collisions`) + testes 9-10.
4. `build_manifest_entry_cmd` + `execute_manifest_workflow` + sumário/log +
   testes 11-16.
5. `run_manifest_workflow` + menu principal + opção `[P]` em
   `run_free_compress` + integração com repeat + testes 17-18.
6. Docs: seção "Manifest System" no README.md e em
   `docs/README_convert_tiff_py.md`; nota de compatibilidade (manifesto só
   garantido com a versão que o gerou).

## 8. Riscos e mitigações

- **Modo 8 deleta fontes** → gate único pré-run + `-DeleteSource` só por entry
  modo 8 + teste 11. Risco residual: baixo.
- **Excel corrompendo encoding** → BOM na escrita + recusa de ANSI na leitura.
  Risco residual: baixo.
- **Colisões entre entries** (cada entry é um processo separado, nenhum vê o
  outro) → scan condicional + overlap check. Risco residual: baixo para modos
  per-source; médio para modo 2 se o usuário editar destinos à mão — mitigado
  pelo scan e pelo fail-closed.
- **Logs filhos fora do lugar** (`$PWD.Path` no `.ps1`) → aceitar o
  comportamento atual no MVP; o log combinado do wrapper aponta os caminhos.
- **Regressão no repeat** → repeat de manifesto usa caminho próprio; runs
  normais continuam com `last_run_commands` (teste 18 cobre a divisão).
