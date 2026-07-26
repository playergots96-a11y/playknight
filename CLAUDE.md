# AutoStock Editor — project context

## Language rules
- Reply to the user in RUSSIAN.
- All code, variable names, and code comments stay in ENGLISH.

## What this app is
Python desktop app (customtkinter GUI) that turns a voiceover into a ready
DaVinci Resolve timeline fully covered with stock footage:

1. Input: audio (.mp3/.wav) AND/OR text script (.txt) — either one is enough.
   - Audio -> local `openai-whisper` (model "base") with
     `word_timestamps=True` (TypeError fallback for ancient versions).
     Segments carry a `words` list; `words_in_scene` assigns each word
     to the scene containing its midpoint. CRITICAL for relevance:
     segments span 5-15s (several 3s scenes) — without word timing every
     scene saw the same whole sentence and got an unrelated query.
   - Text-only -> duration estimated at ~2.5 words/sec with synthesized
     per-word timing (same word-level scene texts), no audio track.
2. SEMANTIC scenes (user requirement - no fixed scene length):
   `build_units` splits narration into phrase units (~1.5-4s, breaks at
   punctuation after `UNIT_BREAK`=2s, hard cap `UNIT_MAX`=4s); the LLM
   labels every unit; `merge_units_into_scenes` merges CONSECUTIVE
   units with the SAME query into one scene - the stock stays on
   screen while the narration stays on the topic. `SCENE_MAX`=12s per
   stretch (longer topics chain several stocks), `SCENE_MIN`=2s merges
   tiny scenes into a neighbor, `PHOTO_SCENE_MAX`=5s converts long
   photo scenes to video. Boundaries are contiguous 0..total (a scene
   runs until the next scene's speech starts).
3. OpenRouter LLM (chain `LLM_MODELS`: deepseek-chat:free ->
   llama-3.3-70b:free -> qwen-2.5-72b:free; (model, key) combos tried in
   order, the working pair is remembered) returns per scene:
   `{"q": "<2-3 word stock query>", "m": "video"|"photo"}`. If EVERY
   combo fails, a loud banner is logged (bad key = the #1 real-world
   cause of irrelevant stocks) and the local fallback `keyword_query`
   kicks in: first 3 CONTENT words (EN+RU `_STOPWORDS` filtered).
   Prompt logic (user-specified, do not water down): use GENERAL
   popular stock concepts, never literal details - narration about a
   tree/logs -> 'forest', about people doing things -> 'people
   working' / 'many people', 1-2 simple English words; abstractions
   mapped to the nearest BROAD filmable image (promise -> 'two people
   talking'); consecutive units on ONE topic must get an IDENTICAL
   query (that is what merges them into one long scene, and the
   library pool guarantees different clips for repeated queries); the
   query changes only when the narration topic changes. Few-shot
   example included. Deliberate repetition of the same query is FINE -
   do not re-add a "variety" rule to the prompt.
   Scenes are sent in chunks of `LLM_CHUNK`=40 (long lists truncate
   free-tier replies); each chunk retries keys starting from the last
   one that worked; a failed chunk falls back per-scene to keywords.
   Target media mix ~70% video / 30% photo, hard cap 40% photo
   (`_balance_media`). Fallback without keys: local keyword extraction.
4. LIBRARY-FIRST POOLS (user requirement): every scene query maps to a
   keyword folder which `ensure_folder_stocked` fills up to
   `FOLDER_MIN_VIDEOS`=7 videos + `FOLDER_MIN_PHOTOS`=3 photos on the
   FOLDER's own theme, with sequential ASCII names Forest01.mp4 ...
   Forest10.jpg (`folder_base_name` CamelCases the slug). Scenes pick
   from the pool; when a pool is drained by the repeat rules it is
   grown by `FOLDER_TOPUP`=3 instead of repeating early; per-scene
   `_download_new_stock` is only a last resort. Each folder has a
   `manifest.json` ({file name: pexels id}, cached in
   `_MANIFEST_CACHE`): no duplicate downloads into a folder, and
   `stock_key` resolves ids through it so repeat rules recognize the
   same stock under any file name. `_folder_ladder` (full theme ->
   first 2 words -> head word) NEVER broadens to the generic query -
   folder content must stay on the folder's topic.
   DURATION-AWARE picking: manifest entries are {"id", "dur",
   "probed"} (legacy bare ids still parse); `_stock_duration` prefers
   the ffprobe MEASUREMENT (persisted with "probed": true) over the
   Pexels API int - the API rounds up and 54 of 126 pooled files had
   wrong durs, which made Resolve report insufficient media ("не
   хватает кадров"); videos with API duration < `STOCK_MIN_DURATION`=4s
   never enter a pool. `pick_from_library(need=...)` prefers videos
   long enough for the scene; if the picked video still cannot cover
   the scene (shortfall > `CHAIN_TOLERANCE`=0.25s), fetch_stocks CUTS
   the scene at the stock's end and CHAINS a different stock for the
   rest (repeat rules forbid the same one back-to-back); chained
   pieces are never shorter than `CHAIN_MIN_PIECE`=1s.
   Folder matching (word overlap, plural normalization): exact word-set
   match wins; a MORE specific folder matches a broader query ('city'
   -> 'city_night'); a LESS specific folder needs >=2 shared words
   (single-word folders like 'old'/'man' must not swallow multi-word
   queries), EXCEPT a single-word folder equal to the query's HEAD
   (last) word ('diary' <- 'old diary'). Cyrillic queries get Pexels
   `locale=ru-RU`.
   REPEAT RULES (user requirements, all hard): one stock (identity =
   `stock_key()`: Pexels id parsed from `pexels_<id>` filename, else
   absolute path) appears at most `MAX_STOCK_USES`=2 times per timeline
   AND a repeat must sit more than `REPEAT_MIN_GAP`=6 other scenes away
   from its previous appearance (never back-to-back). Enforced by
   `StockUsage` (counts + last_pos + current pos). A repeat is a LAST
   resort: if the best library candidate would repeat and Pexels keys
   exist, a fresh download is attempted first; the gap (never the cap)
   is relaxed only in library-only mode with no keys. Less-used
   candidates win, farther repeats beat recent ones, ties randomized. Pexels searches are cached per
   (kind, query, page) in `PexelsClient._cache` (rate-limit friendly)
   and paginate up to `MAX_SEARCH_PAGES`=3 deeper pages on demand when
   page 1 is exhausted by the cap/filters. `PexelsKeysError` (all keys
   dead) aborts the run instead of being swallowed per-query.
   Videos: strict 16:9 landscape, >=720p, prefer closest to 1080p, soft
   preference for ~60fps. Photos: Pexels CDN server-side crop to exactly
   1920x1080 (`?w=1920&h=1080&fit=crop`). Query ladder on miss stays
   ON TOPIC: full query -> first 2 words -> head word ('old diary' ->
   'diary') -> first word -> "cinematic landscape" last; video<->photo
   cross-fallback so a scene is never empty.
5. Output: `.fcpxml` v1.8 (NOT 1.9+!) imported in Resolve via
   File -> Import Timeline. 1920x1080 @ 60fps timeline, times as
   rational frames `N/60s`. Every unique media file is declared as
   exactly ONE `<asset>` (repeated stocks are re-referenced, never
   re-declared with a duplicate src); all files are existence-checked
   before writing (missing -> RuntimeError), non-ASCII paths produce a
   log warning. Photos are assets with `duration="0s"`, a
   rate-undefined format `r2`, and `<video>` spine elements. Voiceover
   attached to the first spine item on `lane="-1"`; with "copy stocks"
   enabled the voiceover is copied into `stock_media/voiceover.<ext>`
   and the fcpxml references the copy.

## Files
- `main.py` — the whole app (~1450 lines): constants/theme, pipeline
  functions, PexelsClient, FCPXML generator, `run_pipeline`, custom
  tkinter widgets (GradientButton, GradientProgress), class App.
- `requirements.txt` — customtkinter, openai-whisper, openai, requests.
- `create_library_folders.py` — optional helper to pre-create keyword
  folders (`--starter` = popular set).
- `build_exe.bat` — PyInstaller onedir build: `--noconsole`,
  `--collect-all customtkinter --collect-all whisper`, icon.ico.
  Result: `dist/AutoStock Editor/AutoStock Editor.exe`.
- `AutoStock Editor.vbs` — no-console launcher via pythonw (no build).
- `icon.ico` — app icon (also loaded as the window icon at runtime).
- `config.json` — auto-saved user settings, CONTAINS API KEYS IN PLAIN
  TEXT. Never commit it, never print its contents.
- `stock_library/` — reusable stock pools (7 video + 3 photo per
  keyword folder, named Forest01... + manifest.json with Pexels ids).
  Do not rename/move while generated projects are in use (fcpxml holds
  absolute paths).
- `build/`, `dist/` — PyInstaller artifacts; ignore for code work.

## Hard-won gotchas (do NOT regress these)
- FCPXML must stay version 1.8: in 1.9+ `src` moved to `<media-rep>`,
  and Resolve silently rejects the file. ("Not a supported file type"
  happens when users import via Import Project instead — correct path is
  File -> Import Timeline.)
- File URIs are written WITHOUT percent-encoding
  (`file://localhost/C:/...`, raw Cyrillic/spaces, `file_src()` helper).
  `pathlib.as_uri()` percent-encoding made Resolve on Windows fail to
  find all clips ("9 of 9 clips were not yet found").
- Windowed PyInstaller build: `sys.stdout`/`sys.stderr` are None ->
  patched to devnull at import time (Whisper/tqdm crash otherwise:
  "'NoneType' object has no attribute 'write'"). Child processes get
  CREATE_NO_WINDOW so ffmpeg doesn't flash console windows.
- `app_dir()` = folder of the .exe when frozen (NOT `_internal`),
  else folder of main.py. config.json and the default library live there.
  Legacy config from `_internal` is migrated on load.
- Worker thread must NEVER touch tkinter. All worker->GUI communication
  goes through `self.ui_queue` (("log"|"progress"|"done"), payload),
  drained by `_drain_ui_queue` on the main thread every 100 ms.
- Downloads are atomic: stream to `<name>.part`, then rename. `.part`
  files are invisible to the library. Cancel (threading.Event) is
  checked between steps, per scene, and per downloaded chunk; partial
  files are removed. Whisper transcription itself cannot be interrupted
  mid-call.
- Clipboard: tkinter hotkeys are keysym-bound, so Ctrl+V fails in the
  Russian keyboard layout; `_enable_clipboard_support` matches physical
  keycodes (86/67/88/65) + Cyrillic keysyms and adds a right-click menu.
- Exactly 5 Pexels key fields (primary + 4 backups). PexelsClient
  rotates keys on HTTP 401/403/429. OpenRouter has 2 fields with
  sequential fallback, then local keyword fallback.
- `slugify()` transliterates Cyrillic to ASCII (`лес` -> `les`): new
  library folders must stay pure ASCII so fcpxml media paths are ASCII
  (non-ASCII paths are the prime suspect for Resolve "media offline").
  Old Cyrillic folders still match Cyrillic queries via `_norm_words`.
- `.txt` scripts are read via `read_text_best_effort` (utf-8-sig ->
  utf-8 -> cp1251): plain utf-8 + errors="ignore" silently deleted ALL
  Cyrillic from cp1251 files.
- Do not reintroduce per-scene `<asset>` declarations in fcpxml: a
  duplicated src across assets confuses Resolve's media linking. One
  unique file = one asset id (see `generate_fcpxml` pass 1).
- Pexels stock audio is intentionally NOT imported (video assets are
  declared `hasVideo` only) — only the voiceover is on the timeline.

## UI (customtkinter, mockup-driven)
Near-black theme (#16161d bg, #1e1e27 cards), pink/violet accents,
pink placeholders. Layout: header; 2-column grid (API KEYS card left:
2 OpenRouter + 5 Pexels entries; INPUTS card right: audio + script pill
rows); full-width OUTPUTS & OPTIONS (output folder, stock library,
copy-to-project switch); full-width ACTIONS & TERMINAL (gradient
Generate button + Cancel, gradient progress bar, log console).
GradientButton/GradientProgress are hand-drawn on tk.Canvas (rounded
gradient via slice fill + corner pieslices) — customtkinter has no
native gradients. Settings auto-save on Generate AND on window close.

## Testing conventions
No GUI needed for logic work: import main.py with stubbed
customtkinter/tkinter/requests modules (import stops at class App —
expected), then unit-test pipeline functions directly. Always verify
generated fcpxml with `xml.dom.minidom.parseString`. Keep
`python -m py_compile main.py` green after every edit.
