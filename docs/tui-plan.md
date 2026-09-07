# Piano: `spotiflac --tui` sostituisce `--interactive`

Documento di lavoro. Riferimento visivo: [MovieBox-Tui](https://github.com/mesamirh/MovieBox-Tui)
(Rust + Ratatui) — l'equivalente Python è [Textual](https://textual.textualize.io/).

Righe di codice citate: verificate sul branch `3.9.1`.

**Obiettivo:** `--tui` diventa l'unica modalità guidata. `interactive.py`
(~1800 righe) viene deprecato e poi rimosso.

---

## Stato

| Fase | Stato |
| --- | --- |
| 0 · Disaccoppiare l'output | **fatta** — `core/output_sink.py` |
| 1 · Scheletro `--tui` | **fatta** — `SpotiFLAC/tui/`, dipendenza base |
| 2 · Config → `cfg` | **fatta** — `tui/config_state.py` + test golden |
| 3 · Coda live | **fatta** — `tui/runner.py`, `tui/queue_view.py` |
| 4 · Parità con la GUI | **fatta** — Ricerca, Health, Profili, History, Estensioni |
| 5 · Rifiniture | **fatta** — temi, `?`, `/`, `j`/`k`, log `Ctrl+L` |
| 6.1 · Estrarre gli helper | **fatta** |
| 6.2 · Ripuntare i test | **fatta** |
| 6.3 · Deprecare | **fatta** — `--interactive` avvisa e apre `--tui` |
| 6.4 · Rimuovere `interactive.py` | **fatta** — il file non esiste più |

Fuori dal piano ma emerso strada facendo:

- `launcher.run_download_from_cfg()` — i 44 kwargs di `_run_download_async`
  esistevano in un solo call site, dentro il ramo `--interactive`. Estratti,
  così wizard e TUI ne condividono uno solo, ed è da lì che il test golden
  legge le chiavi.
- `api_mixins/search.py` — `search_provider` esisteva **in due copie** da ~95
  righe dentro `app.py` (una nel metodo, una nel thread). Unificate.
- `format_command()` raggruppa i flag con i loro valori invece di una riga per
  token: il wizard lo stampava una volta, la TUI lo ridisegna di continuo.

La §10 è coperta: il pannello **Tracks** è la `DataTable` con selezione
multipla a spazio. La logica sta in `core/tracklist.py`, condivisa con la GUI
— selezionare tutto restituisce l'URL della raccolta (una sola risoluzione,
ordinamento e numerazione dell'album intatti), selezionarne una parte
restituisce la lista dei link per traccia. `_run_download_async` accetta
entrambi, ed è ciò che rende possibili tutte e due le strade.

Anche la grafica è stata rifatta sul riferimento MovieBox: wordmark
ANSI-Shadow, bordi arrotondati, titoli `✦  Nome` con tag d'accento, badge a
fondo pieno, hint `[key] action`, i 9 temi con Mocha di default.

---

## 1. Perché è fattibile

Il progetto ha già quasi tutta l'infrastruttura che una TUI richiede, perché è
stata costruita per la GUI pywebview e per il frontend web.

**Il contratto è già pulito.** `run_interactive()` (`SpotiFLAC/interactive.py:972`)
è async e restituisce un dict `cfg`. Il launcher (`SpotiFLAC/launcher.py:2330`)
prende quel dict e lo spalma su `_run_download_async(...)`. La TUI deve solo
produrre **lo stesso dict**: zero refactoring del motore di download.

**Il canale progressi strutturato esiste già.** `DownloadBroadcaster().subscribe(queue)`
(`SpotiFLAC/core/progress.py:239`) emette eventi dict con `downloads`, `queue`,
`latest_completed`, `total_downloaded`. Oggi lo consuma la GUI. La TUI si
iscrive alla stessa coda asyncio — **non serve parsare l'output di tqdm**.
Questo è il punto che di solito uccide i porting a TUI, e qui è già risolto.

**La superficie funzionale è già Python-callable.** Gli `api_mixins` più
`SpotiFLAC_API` (`SpotiFLAC/app.py:165`) espongono `search_provider`,
`get_history`, `get_profiles`, `browse_folder`, health check, registries —
tutti metodi che tornano dict.

**Tutto è già asyncio**, e Textual è async-native.

---

## 2. Perché sostituire e non affiancare

Il costo vero è il **fan-out dei flag**: `_run_download_async` prende una
quarantina di kwargs, e ogni nuovo flag di download va cablato a mano in
argparse, nel wizard (15 sezioni), nella GUI e nel web. Un quinto frontend
permanente sarebbe un posto in più dove dimenticarsene. Due UI di
configurazione non le mantiene bene nessuno: quella meno usata marcisce.

Le obiezioni alla sostituzione non reggono:

- **Dipendenza.** `pywebview>=5.0.0` è già una dipendenza base, ed è molto più
  pesante di Textual: tira dentro binding nativi (pyobjc su macOS, GTK/Qt su
  Linux). Textual è puro Python (rich, markdown-it-py, platformdirs). Può
  entrare nelle dipendenze base senza rimorsi.
- **Rotture.** Nulla di strutturale dipende da `--interactive`: non compare in
  Dockerfile, docker-entrypoint.sh o CI. Lo citano solo `launcher.py` e quattro
  pagine di documentazione.

**Cosa resta scoperto:** screen reader, invocazione via pipe/heredoc, terminali
molto limitati. In tutti e tre i casi la risposta giusta non è un secondo
wizard, è la **CLI** — che esiste già ed è più adatta di entrambi. Non a caso
`_print_cli_command` (`interactive.py:1685`) è nato proprio per traghettare la
gente dal wizard alla CLI.

### Vincolo architetturale

**La TUI non deve importare `SpotiFLAC.app`.** Quel modulo fa `import webview`
a livello modulo (`SpotiFLAC/app.py:16`) e `SpotiFLAC_API` è sincrono e pieno di
`threading`. La TUI riusa invece gli `api_mixins/*` (verificato: nessuno importa
pywebview) e le funzioni `core/*`.

---

## 3. Fase 0 — Disaccoppiare l'output dal terminale

> Il vero blocco tecnico. Va fatta per prima e si merge da sola, senza che
> nessun frontend cambi comportamento.

**Problema:** `core/console.py:12` e i 7 `safe_tqdm_write` in `downloader.py`
(righe 403, 762, 772, 863, 972, 1249) scrivono diretto su stdout/stderr. In
alternate screen corrompono il rendering.

**Lavoro:**

1. Nuovo `SpotiFLAC/core/output_sink.py`: sink pluggable a livello modulo,
   default = comportamento attuale (`tqdm.write`).
2. `safe_print`, `safe_tqdm_write` (`core/progress.py:52-60`) e `console._write`
   passano dal sink invece di chiamare `tqdm` direttamente.
3. `progress_bars_enabled()` (`core/progress.py:36`) guadagna un terzo stato:
   sink attivo → `False`. Metà del lavoro è già lì, la funzione sa già
   distinguere tty da log.
4. `ProgressManager` (`core/progress.py:476`): con sink attivo non istanzia
   nessuna `tqdm`, si limita agli eventi.
5. Handler di logging instradabile — il pattern esiste già come `UILogHandler`
   (`app.py:132`), va generalizzato fuori da `app.py`.

**Test:** con sink attivo, nessun byte su stdout/stderr durante un download
simulato. Più i test esistenti che devono restare verdi:
`tests/test_progress_and_csv_counters.py`, `tests/test_progress_manager_loops.py`.

**Deliverable:** zero cambiamenti visibili. È refactoring puro.

---

## 4. Fase 1 — Scheletro `--tui`

1. `textual>=1.0` nelle **dipendenze base** di `pyproject.toml` (pinnare la
   versione corrente al momento dell'install), non in un extra: la TUI diventerà
   l'unica modalità guidata, quindi non può essere opzionale. Tenere
   `requirements.txt` allineato.
2. Nuovo package `SpotiFLAC/tui/` — `__init__.py`, `app.py` con
   `class SpotiFLACTui(App)`.
3. Dispatch in `launcher.py` accanto a `--gui` (`launcher.py:1745`). Non serve
   il pattern try/`ImportError` di `--web` (`launcher.py:1769-1784`): quello
   esiste perché FastAPI sta in un extra, e qui non è il caso.

**Deliverable:** `spotiflac --tui` apre una schermata vuota con sidebar, temi
commutabili, e si chiude pulita.

---

## 5. Fase 2 — Schermata Config → produce `cfg`

Il wizard ha 15 sezioni sostanziali (`interactive.py`, righe 1011→1639):
URL/CSV, output dir, custom output path, services, quality, transcoding,
filename format, organization, lyrics, metadata enrichment, retry, concurrency,
timeout, post-download action, Qobuz local API. Più le tre opzionali: history,
profili, registries.

1. `tui/config_state.py`: una dataclass che tiene quello stato in forma
   **dichiarativa** (non sequenziale) con `to_cfg() -> dict`.
2. Il dict deve combaciare con quello che `launcher.py:2346` spalma su
   `_run_download_async`. **Test golden** che confronta le chiavi prodotte con
   quelle consumate lì — è l'unica difesa contro il drift quando qualcuno
   aggiunge un flag. Finché il wizard esiste, il test confronta anche `to_cfg()`
   con il `cfg` del wizard: è la rete di sicurezza della migrazione, e ti dice
   se hai perso un'opzione per strada.
3. Riuso diretto, senza riscrivere: `_TRANSCODE_CHOICES` (`interactive.py:34`),
   `_installed_service_options` (`interactive.py:196`), `normalize_quality`,
   `_print_cli_command` (`interactive.py:1685`) → che diventa un pannello
   "Equivalent CLI command" aggiornato in tempo reale mentre cambi le opzioni.
   È una feature gratuita e molto bella. Vedi la Fase 6 per dove finiscono
   questi helper.

**Rischio:** è la fetta di lavoro più grossa, ~700 righe di logica sequenziale
da ripensare come stato. Non è un porting meccanico.

---

## 6. Fase 3 — Coda download live (MVP)

1. Widget coda che fa `DownloadBroadcaster().subscribe(asyncio.Queue)`
   (`core/progress.py:239`) e consuma gli eventi: `downloads`, `queue`,
   `latest_completed`, `total_downloaded`.
2. Una `ProgressBar` per traccia + barra master. Nessun parsing di tqdm: gli
   eventi sono già dict strutturati.
3. Lancio di `_run_download_async` (`launcher.py:1474`) dentro un worker
   Textual, con il sink della Fase 0 attivo.

**Deliverable:** a questo punto `--tui` fa già tutto ciò che fa `--interactive`.
È il momento in cui scatta la Fase 6.

---

## 7. Fase 4 — Parità con la GUI

Pannelli aggiuntivi, in ordine di valore:

| Pannello | Fonte già esistente |
| --- | --- |
| Ricerca | `search_provider` (`app.py:676`) — **da estrarre in un mixin**, oggi usa thread ed è in `app.py` |
| Health check | `core/health_check.run_health_check`, già usato a `interactive.py:249` |
| Profili | `get_profiles` / `load_profile_data` / `save_profile_data` / `delete_profile_data` (`app.py:642-960`) |
| History | `get_history` / `remove_history_item` (`app.py:634`, `:909`) |
| Estensioni | `api_mixins/discovery.py` + `_manage_registries_section` (`interactive.py:698`) |

**Nota di rischio:** i metodi in `app.py` sono sincroni. Vanno estratti in mixin
(il docstring di `SpotiFLAC_API` dice già che è la direzione voluta) oppure
chiamati con `run_worker(thread=True)`. Estrarli è meglio: ne beneficia anche
il web.

---

## 8. Fase 5 — Rifiniture

Temi built-in di Textual (`catppuccin-mocha`, `nord`, `dracula`, `gruvbox`,
`tokyo-night` — gli stessi che elenca MovieBox), help contestuale `?`,
keybinding vim `j`/`k`, `/` per la ricerca, mouse, log a scomparsa.

---

## 9. Fase 6 — Deprecazione e rimozione di `interactive.py`

### 9.1 Estrarre gli helper condivisi (**prerequisito**)

`interactive.py` non è solo il wizard: contiene logica riusata altrove. Va
spostata in un modulo condiviso **prima** di poter cancellare il file.

| Helper | Righe | Chi lo usa oggi oltre al wizard |
| --- | --- | --- |
| `_print_cli_command` | 1685 | `tests/test_transcode_lossless.py:624`, `tests/test_interactive_cli_command.py` |
| `_TRANSCODE_CHOICES` / `_transcode_label` | 34, 46 | `tests/test_transcode_lossless.py:549` |
| `_installed_service_options` | 196 | `tests/test_smoke.py:151`, `tests/test_download_services.py:124` |
| Picker CSV: `_clean_path_input`, `_looks_like_csv_path`, `_scan_csv_files`, `_csv_scan_dirs`, `_read_csv_document`, `_accept_csv_path`, `_pick_csv_file` | 395-652 | `tests/test_interactive_csv_picker.py` |
| `_pick_from_history` | 294 | — (ma serve al pannello History) |

Destinazione: `SpotiFLAC/core/` per quelli senza I/O da terminale (scan CSV,
scelte transcode, lista servizi), `SpotiFLAC/tui/` per quelli che diventano
widget (picker, history).

### 9.2 Ripuntare i test

**Cinque** file di test importano da `interactive`: `test_interactive_cli_command.py`,
`test_interactive_csv_picker.py`, `test_transcode_lossless.py`, `test_smoke.py`,
`test_download_services.py`. Vanno ripuntati sui nuovi moduli, **non cancellati**.

Nota su `tests/test_download_services.py:124`: asserisce che la lista servizi
del wizard sia identica a quella della GUI. È un test di coerenza fra frontend
— va ripuntato sulla TUI e tenuto, perché è esattamente la classe di test che
protegge dal fan-out dei flag descritto nella sezione 2.

### 9.3 Deprecare

Al rilascio dell'MVP (fine Fase 3):

- `--interactive` diventa un alias di `--tui` con un `DeprecationWarning`.
- Aggiornare le quattro pagine che lo citano: `docs/guide/quick-start.md`,
  `docs/guide/configuration.md`, `docs/guide/csv.md`,
  `docs/guide/api-reference.md`, più `docs/index.html`.
- Nuova pagina `docs/guide/tui.md`.
- **Nessun wizard di fallback.** Se il terminale non regge la TUI, il fallback
  è un messaggio che rimanda alla CLI, non una seconda UI da mantenere in
  eterno.

### 9.4 Rimuovere

Una release dopo: eliminare `SpotiFLAC/interactive.py` e l'import a
`launcher.py:38`. Il progetto torna a quattro frontend invece di cinque.

---

## 10. Layout previsto

Sul modello MovieBox:

- **Sidebar** con le modalità: Download / Cerca / Coda / Estensioni / Profili / Impostazioni
- **Pannello centrale**: `DataTable` con i brani, selezione multipla a spazio
- **Pannello coda**: una `ProgressBar` per traccia, alimentata dal broadcaster
- **Barra comandi**: `/` per la ricerca, `?` per l'help
- **Log a scomparsa**: `Ctrl+L`

---

## 11. Sequenza e stime

Fase 0 da sola (nessun rischio, sblocca tutto) → Fasi 1+2+3 come un unico
rilascio "MVP TUI", subito seguito da 6.1→6.3 (deprecazione) → poi 4 e 5 in
modo incrementale → infine 6.4 (rimozione) una release dopo.

| Blocco | Stima |
| --- | --- |
| Fase 0 | mezza giornata |
| MVP (Fasi 1→3) | un paio di giorni |
| Fase 6.1 + 6.2 (estrazione helper + test) | una giornata |
| Fase 4 | qualche giorno, distribuibile |
