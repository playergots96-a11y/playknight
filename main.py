"""
AutoStock Editor v2
-------------------
Desktop app that turns a voiceover (audio and/or text script) into a ready
DaVinci Resolve timeline (.fcpxml) fully covered with relevant stock footage.

Pipeline:
  1. Transcribe audio with local openai-whisper (timestamps), OR estimate
     timing from the text script if no audio is provided.
  2. Split the narration into phrase units (word timestamps); the LLM
     labels each unit, and consecutive units on one topic merge into a
     SEMANTIC scene: the stock stays on screen exactly while the
     narration stays on the topic (stocks chain when one is too short).
  3. Ask an LLM (OpenRouter) for ONE broad, popular stock search query per
     scene (general visual concepts, not literal details).
  4. LIBRARY-FIRST stocks: every scene query maps to a keyword folder
     in the local library, which is stocked up to 7 videos + 3 photos
     on its theme (sequential names: Forest01.mp4 ... Forest10.jpg) and
     grown further when a long timeline drains it. Scenes pick from the
     pool (randomized), honoring the repeat rules: one stock at most
     MAX_STOCK_USES times per timeline and never closer than
     REPEAT_MIN_GAP scenes to its previous appearance.
  5. Generate .fcpxml (v1.8) importable via
     File -> Import Timeline in DaVinci Resolve.

GUI: customtkinter, dark gray + orange, minimalist. Heavy work runs in a
background thread; primary/backup API keys are supported and auto-saved
to config.json next to this script.
"""

import os
import re
import sys
import json
import queue
import random
import shutil
import platform
import threading
import subprocess
import traceback
from pathlib import Path

import requests
import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog


# In a windowed (--noconsole) PyInstaller build there is no terminal, so
# sys.stdout / sys.stderr are None. Whisper/tqdm try to write progress to
# them and crash with "'NoneType' object has no attribute 'write'".
# Route the streams to devnull so console output is silently discarded.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


# In a windowed build, child processes (ffmpeg launched by Whisper) would
# each flash a black console window. Force CREATE_NO_WINDOW on Windows.
if platform.system() == "Windows":
    _OrigPopen = subprocess.Popen

    class _NoWindowPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = (kwargs.get("creationflags", 0)
                                       | 0x08000000)  # CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen


def app_dir() -> Path:
    """
    Folder where the app stores config.json and the default stock library.
    For a PyInstaller build this is the folder with the .exe (NOT the
    hidden _internal folder); for plain Python - the folder with main.py.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent



# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

FPS = 60                                  # timeline frame rate
WORDS_PER_SEC = 2.5                       # reading speed for text-only mode
WHISPER_MODEL_NAME = "base"

# Semantic scenes: narration is split into small phrase units; the LLM
# gives consecutive units of one topic the SAME query and they merge
# into a single scene - the stock stays on screen for as long as the
# narration stays on the topic. Longer topics are chained from several
# stocks (SCENE_MAX per stretch), and a picked video that cannot cover
# its scene gets a second stock chained after it automatically.
UNIT_MAX = 4.0          # max narration unit length, seconds
UNIT_BREAK = 2.0        # punctuation may end a unit after this length
SCENE_MIN = 2.0         # scenes shorter than this merge into a neighbor
SCENE_MAX = 12.0        # one stock never stretches longer than this
PHOTO_SCENE_MAX = 5.0   # photo scenes longer than this become video
CHAIN_TOLERANCE = 0.25  # chain a 2nd stock when shortfall exceeds this
CHAIN_MIN_PIECE = 1.0   # never create chained pieces shorter than this

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Tried in order until one answers: free models rate-limit and go down
# often, and a failed LLM used to silently degrade every query into
# low-relevance local keywords.
LLM_MODELS = [
    "deepseek/deepseek-chat:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
]
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
GENERIC_QUERY = "cinematic landscape"     # last-chance search term

# Media mix: LLM aims for ~70% video / 30% photo; hard cap on photos below.
PHOTO_TARGET = 0.30
PHOTO_MAX = 0.40

# Repetition & variety: one stock (by Pexels id / file) may appear at most
# MAX_STOCK_USES times per timeline, and a repeat must sit at least
# REPEAT_MIN_GAP other scenes away from its previous appearance (never
# back-to-back). When page 1 of a search is exhausted by these rules or
# the quality filters, deeper pages are fetched on demand.
MAX_STOCK_USES = 2
REPEAT_MIN_GAP = 6
MAX_SEARCH_PAGES = 3
VIDEO_PER_PAGE = 30
PHOTO_PER_PAGE = 20

# Scenes per LLM request: very long scene lists make free-tier models
# truncate the JSON reply, so long timelines are sent in chunks.
LLM_CHUNK = 40

# Local stock library: keyword folders with reusable footage.
DEFAULT_LIBRARY = app_dir() / "stock_library"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# Library folder pools: every keyword folder is stocked up to at least
# FOLDER_MIN_VIDEOS videos + FOLDER_MIN_PHOTOS photos on its own theme,
# named sequentially (Forest01.mp4 ... Forest10.jpg). Scenes pick from
# the pool instead of downloading per scene; FOLDER_TOPUP more files are
# fetched when a pool runs dry. manifest.json inside each folder maps
# file names to Pexels ids (no duplicate downloads, repeat rules see the
# same stock under any name).
FOLDER_MIN_VIDEOS = 7
FOLDER_MIN_PHOTOS = 3
FOLDER_TOPUP = 3
MANIFEST_NAME = "manifest.json"
# Videos shorter than this never enter a pool: scenes are ~3s (up to
# ~3.8s with a merged tail) and a shorter stock makes Resolve complain
# about insufficient media in the clip.
STOCK_MIN_DURATION = 4.0

APP_TITLE = "AutoStock Editor"
CONFIG_FILE = app_dir() / "config.json"

# --- Theme: near-black UI with soft pink/violet accents (mockup style) ---
COL_BG = "#16161d"          # window background
COL_CARD = "#1e1e27"        # card surface
COL_FIELD = "#282833"       # inputs / pill rows / secondary buttons
COL_HOVER = "#333340"
COL_BORDER = "#3a3a4a"
COL_TEXT = "#f4f4f8"
COL_MUTED = "#8f8fa3"
COL_LOG_BG = "#14141b"
PLACEHOLDER_PINK = "#cf8ab8"

ACC_PINK = "#ff5ca8"
ACC_VIOLET = "#a78bfa"
ACC_CYAN = "#4dd8c7"
ACC_ORANGE = "#ffab5e"

# gradient stops as (position, (r, g, b))
GRAD_MAIN = [(0.0, (255, 92, 168)), (0.55, (167, 139, 250)),
             (1.0, (77, 216, 199))]
GRAD_BTN = [(0.0, (242, 150, 200)), (1.0, (166, 140, 250))]

FONT_UI = "Segoe UI"
FONT_HEAD = "Segoe UI Semibold"
FONT_MONO = "Consolas"


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

class CancelledError(Exception):
    """Raised when the user presses Cancel during generation."""


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("cancelled by user")


def sec_to_frames(seconds: float) -> int:
    """Convert seconds (float) to whole timeline frames."""
    return max(0, int(round(seconds * FPS)))


def frames_to_rational(frames: int) -> str:
    """FCPXML rational time string, e.g. 240/60s."""
    return f"{frames}/{FPS}s"


def file_src(path: Path) -> str:
    """
    Build a file URI for FCPXML that DaVinci Resolve reliably resolves.
    pathlib's as_uri() percent-encodes spaces and non-Latin characters
    (%20, %D0%9F...), which Resolve's XML importer often fails to decode
    on Windows, leading to "clips were not found". We emit the absolute
    path raw, with forward slashes and no percent-encoding; xml_escape()
    still handles XML-special characters like &.
    """
    raw = str(path.resolve()).replace("\\", "/")
    if not raw.startswith("/"):
        raw = "/" + raw          # Windows drive letters: /C:/...
    return "file://localhost" + raw


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def open_folder(path: str):
    """Open a folder in the OS file explorer (cross-platform)."""
    if platform.system() == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def read_text_best_effort(path: Path) -> str:
    """
    Read a user text file that may be UTF-8 (with or without BOM) or a
    Windows Cyrillic codepage (cp1251). Decoding cp1251 content as UTF-8
    with errors='ignore' would silently strip every Cyrillic letter and
    ruin the whole script, so real decoding attempts come first.
    """
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_config() -> dict:
    # also look in the legacy location (next to main.py inside _internal)
    for path in (CONFIG_FILE, Path(__file__).with_name("config.json")):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(cfg, dict) or not cfg:
            continue
        # migrate the old two-field Pexels config into the key list
        if "pexels_keys" not in cfg:
            legacy = [cfg.get("pexels_key", ""), cfg.get("pexels_key2", "")]
            cfg["pexels_keys"] = [k for k in legacy if k]
        return cfg
    return {}


def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass  # config persistence is best-effort


# ----------------------------------------------------------------------------
# Step 1: timing sources (audio transcription OR text estimation)
# ----------------------------------------------------------------------------

def transcribe_audio(audio_path: str, log) -> list:
    """
    Run local Whisper with per-WORD timestamps. Returns segments:
    [{"start": float, "end": float, "text": str,
      "words": [{"start", "end", "word"}, ...]}, ...]
    Word timestamps are essential: whole segments span 5-15s, i.e.
    several 3-second scenes, and without word timing every scene would
    see the same full sentence and get an unrelated stock query.
    """
    log(f"Loading Whisper model '{WHISPER_MODEL_NAME}' "
        "(first run downloads ~150 MB)...")
    try:
        import whisper  # lazy import so the GUI starts instantly
    except ImportError:
        raise RuntimeError(
            "openai-whisper is not installed. "
            "Run: pip install -r requirements.txt")

    model = whisper.load_model(WHISPER_MODEL_NAME)
    log("Transcribing audio... this can take a while.")
    try:
        result = model.transcribe(audio_path, verbose=False,
                                  word_timestamps=True)
    except TypeError:   # ancient openai-whisper without word timestamps
        result = model.transcribe(audio_path, verbose=False)

    segments = [
        {"start": float(s["start"]), "end": float(s["end"]),
         "text": s["text"].strip(),
         "words": [{"start": float(w["start"]), "end": float(w["end"]),
                    "word": str(w.get("word", ""))}
                   for w in (s.get("words") or [])]}
        for s in result.get("segments", [])
        if s["text"].strip()
    ]
    if not segments:
        raise RuntimeError("Whisper returned no speech segments.")
    log(f"Transcription done: {len(segments)} segments, "
        f"{segments[-1]['end']:.1f}s of speech.")
    return segments


def estimate_segments_from_text(text: str, log) -> tuple:
    """
    Text-only mode: no audio. Estimate timing from reading speed.
    Returns (segments, total_duration).
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text)
                 if s.strip()]
    if not sentences:
        raise RuntimeError("The script file is empty.")

    segments, t = [], 0.0
    for sent in sentences:
        words = sent.split() or [sent]
        dur = len(words) / WORDS_PER_SEC
        # synthesize evenly spaced per-word timing so 3-second scenes
        # get exactly the words "spoken" inside them, like with Whisper
        word_items, wt = [], t
        for w in words:
            word_items.append({"start": round(wt, 3),
                               "end": round(wt + 1 / WORDS_PER_SEC, 3),
                               "word": w})
            wt += 1 / WORDS_PER_SEC
        segments.append({"start": round(t, 2), "end": round(t + dur, 2),
                         "text": sent, "words": word_items})
        t += dur
    log(f"Text-only mode: estimated duration {t:.1f}s "
        f"({len(sentences)} sentences, ~{WORDS_PER_SEC} words/sec).")
    return segments, t


# ----------------------------------------------------------------------------
# Step 2: narration units -> semantic scenes
# ----------------------------------------------------------------------------

def _unit_from_words(ws: list) -> dict:
    return {"start": round(ws[0]["start"], 2),
            "end": round(ws[-1]["end"], 2),
            "text": " ".join(str(w["word"]).strip() for w in ws).strip()}


def build_units(segments: list, log) -> list:
    """
    Split the narration into small phrase units (~1.5-4s): a unit ends at
    punctuation once it is UNIT_BREAK long, or hard-breaks at UNIT_MAX.
    Units are the LLM's working granularity - consecutive units on one
    topic get the same query and later merge into a single scene.
    Fallback without word timestamps: whole segments become units.
    """
    words = [w for s in segments for w in (s.get("words") or [])]
    units = []
    if words:
        cur = []
        for w in words:
            cur.append(w)
            dur = w["end"] - cur[0]["start"]
            token = str(w["word"]).strip()
            if dur >= UNIT_MAX or (dur >= UNIT_BREAK
                                   and token[-1:] in ".!?…,;:"):
                units.append(_unit_from_words(cur))
                cur = []
        if cur:
            units.append(_unit_from_words(cur))
    else:
        units = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                 for s in segments if str(s.get("text", "")).strip()]
    units = [u for u in units if u["text"].strip()]
    if not units:
        raise RuntimeError("The audio/script is too short to build "
                           "a timeline.")
    log(f"Narration split into {len(units)} phrase units.")
    return units


def merge_units_into_scenes(units: list, total_dur: float, log) -> list:
    """
    Turn LLM-labeled units into the final scene list:
      - consecutive units with the SAME query merge into one scene (the
        stock stays on screen while the narration stays on the topic),
        splitting at SCENE_MAX so one stock never stretches too far;
      - scenes shorter than SCENE_MIN merge into the previous scene
        (the first one merges forward);
      - boundaries are made contiguous for the timeline: a scene runs
        until the next scene's speech starts, the first starts at 0,
        the last ends at total_dur (silence belongs to the current image);
      - photo scenes longer than PHOTO_SCENE_MAX become video scenes.
    """
    scenes = []
    for u in units:
        prev = scenes[-1] if scenes else None
        if (prev is not None
                and prev["query"].strip().lower()
                == u["query"].strip().lower()
                and u["end"] - prev["_sstart"] <= SCENE_MAX):
            prev["_send"] = u["end"]
            prev["text"] = (prev["text"] + " " + u["text"]).strip()
        else:
            scenes.append({"query": u["query"], "media": u["media"],
                           "text": u["text"],
                           "_sstart": u["start"], "_send": u["end"]})

    merged = []
    for s in scenes:
        if merged and (s["_send"] - s["_sstart"]) < SCENE_MIN:
            merged[-1]["_send"] = s["_send"]
            merged[-1]["text"] = (merged[-1]["text"] + " "
                                  + s["text"]).strip()
        else:
            merged.append(s)
    if len(merged) > 1 and (merged[0]["_send"] - merged[0]["_sstart"]) \
            < SCENE_MIN:
        merged[1]["_sstart"] = merged[0]["_sstart"]
        merged[1]["text"] = (merged[0]["text"] + " "
                             + merged[1]["text"]).strip()
        merged.pop(0)

    for i, s in enumerate(merged):
        s["start"] = 0.0 if i == 0 else merged[i - 1]["end"]
        if i + 1 < len(merged):
            s["end"] = max(round(merged[i + 1]["_sstart"], 2),
                           s["start"] + 0.5)
        else:
            s["end"] = max(round(total_dur, 2), s["start"] + 0.5)
        del s["_sstart"], s["_send"]
        if s["media"] == "photo" and (s["end"] - s["start"]) \
                > PHOTO_SCENE_MAX:
            s["media"] = "video"
    avg = total_dur / max(1, len(merged))
    log(f"{len(units)} units merged into {len(merged)} scenes "
        f"(avg {avg:.1f}s per scene).")
    return merged


def words_in_scene(scene: dict, segments: list) -> str:
    """
    Collect the words spoken inside a scene's time window. With
    word-level timestamps a word belongs to the scene containing its
    midpoint, so each 3-second scene gets EXACTLY its own narration
    fragment. Fallback (no word data): whole overlapping segments, which
    bleed one sentence into several scenes - much worse for relevance.
    """
    if any(s.get("words") for s in segments):
        picked = []
        for s in segments:
            for w in s.get("words", []):
                mid = (w["start"] + w["end"]) / 2
                if scene["start"] <= mid < scene["end"]:
                    picked.append(w["word"].strip())
        return " ".join(p for p in picked if p).strip()
    return " ".join(
        s["text"] for s in segments
        if s["start"] < scene["end"] and s["end"] > scene["start"]
    ).strip()


# ----------------------------------------------------------------------------
# Step 3: search queries (LLM with key fallback, or local fallback)
# ----------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = (
    "You are a senior stock-footage researcher for YouTube explainer "
    "videos. You receive numbered short narration fragments (units) "
    "with timestamps - each is EXACTLY what is spoken during those "
    "seconds, often a sentence fragment. For EVERY unit output an "
    'object {"q": "<search query>", "m": "video" | "photo"}.\n'
    "\n"
    "TOPIC GROUPING (most important):\n"
    "- While the narration STAYS on one visual topic, give its "
    "consecutive units EXACTLY the same query string, character for "
    "character. Identical consecutive queries merge into ONE longer "
    "scene covered by one stock clip: a topic narrated for 10 seconds "
    "becomes one 10-second stock on screen.\n"
    "- The moment the narration moves to a NEW image (new person, "
    "object or action), switch the query on that exact unit. Never "
    "keep showing the previous image after the text has moved on.\n"
    "- Do not reuse an earlier query for a later NON-consecutive unit "
    "unless the narration truly returns to that image.\n"
    "\n"
    "HOW TO PICK THE IMAGE:\n"
    "- Use GENERAL, popular stock concepts - never literal details. "
    "Ask 'what TOPIC is the narration on right now?' and answer with "
    "a simple popular subject: a tree, logs or lumber -> 'forest'; "
    "people trying or doing things -> 'people working' or 'many "
    "people'; prices or costs -> 'money'; a diary, book or knowledge "
    "-> 'old books'.\n"
    "- Translate abstractions into the nearest BROAD filmable image: "
    "a promise -> 'two people talking', death -> 'burning candle', "
    "success -> 'city sunrise'.\n"
    "- Stick to broad popular categories: forest, nature, ocean, city, "
    "people, family, home, fire, tools, technology, business, money, "
    "food, transport, weather, books.\n"
    "\n"
    "EXAMPLE. Units: 1.'None of the old methods have stopped working' "
    "2.'each still works like in 1890' 3.'today you will learn to "
    "build' 4.'a single log fire' 5.'and which wood to choose'. "
    "Answer queries: 1:'old tools', 2:'old tools', 3:'fireplace', "
    "4:'fireplace', 5:'firewood' - consecutive units on one topic "
    "share one identical query.\n"
    "\n"
    "QUERY RULES:\n"
    "- 1-2 simple English words (3 only if truly needed). ALWAYS write "
    "the query in ENGLISH even when the narration is in another "
    "language.\n"
    "- Avoid brand names, niche jargon, bare verbs and abstract nouns "
    "alone ('success', 'economy').\n"
    "\n"
    "MEDIA TYPE:\n"
    "- 'video' is the default - anything with natural motion.\n"
    "- 'photo' only for SHORT static concepts genuinely hard to convey "
    "with motion: maps, documents, portraits, schematic close-ups. "
    "Topics the narration dwells on for more than ~5 seconds must be "
    "'video'.\n"
    "- Across the whole list aim for roughly 70% video / 30% photo.\n"
    "\n"
    "Respond with ONLY a JSON array of these objects, one per unit, in "
    "order, with EXACTLY as many items as units. No markdown, no text."
)


# Function words filtered out by the local keyword fallback: without the
# filter it produced queries like "None methods have" that returned
# near-random footage whenever the LLM was unavailable.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here is are
was were be been being am do does did done doing have has had having will
would can could should shall may might must not no none of in on at by
for with from to into onto out up down over under again once only just
very too also still each every all any some few more most much many other
such as it its itself they them their theirs we our ours you your yours
he she his her hers him us who whom which what when where why how know
knows knew known make makes made get gets got take takes took see sees
saw say says said went gone going come comes came one two three first
second next last way ways thing things lot bit exactly really actually
и в во не на я с со как а то все она они оно так его но да ты к у же вы
за бы по ее её мне было вот от меня еще ещё нет о из ему теперь когда
даже ну ли если уже или ни быть был была были него до вас нибудь опять
уж вам ведь там потом себя ничего может мы тебя их чем сам чтоб без
будто чего раз тоже себе под будет тогда кто этот того потому этого
какой ним здесь этом почти мой тем чтобы нее неё сейчас куда зачем всех
можно при весь наш свой это эта эти том быть есть надо очень просто
""".split())


def keyword_query(text: str) -> str:
    """
    Local fallback: pick the first few CONTENT words of the narration
    (function words removed, duplicates skipped). Only used when every
    LLM model/key failed - quality is inherently worse than the LLM's.
    """
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text)
    picked = []
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS or any(low == p.lower() for p in picked):
            continue
        picked.append(tok)
        if len(picked) == 3:
            break
    return " ".join(picked) or GENERIC_QUERY


def build_queries(openrouter_keys: list, scenes: list, segments: list, log):
    """
    Fill scene["query"] and scene["media"] ("video" / "photo") for every
    scene. Scenes are sent to the LLM in chunks of LLM_CHUNK (huge lists
    make free-tier models truncate the JSON reply). Each chunk tries every
    OpenRouter key in turn, starting from the last key that worked; scenes
    of a failed chunk fall back to local keyword extraction.
    """
    for scene in scenes:
        scene["text"] = scene.get("text") or words_in_scene(scene, segments)
        scene["media"] = "video"

    keys = [k for k in openrouter_keys if k]
    if not keys:
        log("No OpenRouter key provided - using local keyword queries.")
        for scene in scenes:
            scene["query"] = keyword_query(scene["text"])
        return

    from openai import OpenAI

    llm_ok = 0
    # (model, key) combos tried in order; remember what worked last so
    # later chunks go straight to the healthy pair
    combos = [(m, ki) for m in LLM_MODELS for ki in range(len(keys))]
    combo_start = 0
    n_chunks = (len(scenes) + LLM_CHUNK - 1) // LLM_CHUNK
    for chunk_no, base in enumerate(range(0, len(scenes), LLM_CHUNK), 1):
        chunk = scenes[base:base + LLM_CHUNK]
        scene_lines = "\n".join(
            f'{i}. [{s["start"]:.1f}-{s["end"]:.1f}s] '
            f'{s["text"] or "(no speech)"}'
            for i, s in enumerate(chunk, 1)
        )
        user_prompt = f"SCENES ({len(chunk)} total):\n{scene_lines}"

        data = None
        for attempt in range(len(combos)):
            ci = (combo_start + attempt) % len(combos)
            model_name, ki = combos[ci]
            batch = f" (batch {chunk_no}/{n_chunks})" if n_chunks > 1 else ""
            try:
                log(f"Querying LLM ({model_name}) with OpenRouter "
                    f"key #{ki + 1}{batch}...")
                client = OpenAI(base_url=OPENROUTER_BASE_URL,
                                api_key=keys[ki])
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.4,
                )
                raw = resp.choices[0].message.content.strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
                match = re.search(r"\[.*\]", raw, flags=re.S)
                if not match:
                    raise ValueError("no JSON array in LLM response")
                data = json.loads(match.group(0))
                combo_start = ci        # stick with the pair that worked
                break
            except Exception as e:
                log(f"! {model_name} / key #{ki + 1} failed: {e}")

        if data is None:
            log("! LLM unavailable for this batch - using local keyword "
                "queries for its scenes.")
            for scene in chunk:
                scene["query"] = keyword_query(scene["text"])
            continue

        if len(data) != len(chunk):
            log(f"! LLM returned {len(data)} items for {len(chunk)} "
                "scenes - missing ones fall back to keywords.")
        for i, scene in enumerate(chunk):
            item = data[i] if i < len(data) else {}
            if isinstance(item, dict):
                q = str(item.get("q") or item.get("query") or "").strip()
                m = str(item.get("m") or item.get("media") or "").strip()
            else:
                q, m = str(item).strip(), "video"
            scene["query"] = q or keyword_query(scene["text"])
            scene["media"] = ("photo" if m.lower().startswith("p")
                              else "video")
        llm_ok += len(chunk)

    _balance_media(scenes, log)
    n_photo = sum(1 for s in scenes if s["media"] == "photo")
    log(f"Queries ready: {llm_ok} of {len(scenes)} scenes via the LLM "
        f"({len(scenes) - n_photo} video / {n_photo} photo planned).")
    if llm_ok == 0:
        log("!" * 56)
        log("! EVERY LLM request failed - queries were built from raw")
        log("! keywords and stock relevance will be much worse.")
        log("! Check the OpenRouter API key(s) in the API KEYS card.")
        log("!" * 56)


def _balance_media(scenes: list, log):
    """Cap the photo share at PHOTO_MAX (the LLM sometimes overshoots)."""
    max_photos = int(round(len(scenes) * PHOTO_MAX))
    photo_scenes = [s for s in scenes if s["media"] == "photo"]
    if len(photo_scenes) > max_photos:
        for s in photo_scenes[max_photos:]:
            s["media"] = "video"
        log(f"Photo share capped at {int(PHOTO_MAX * 100)}% "
            f"({max_photos} of {len(scenes)} scenes).")


# ----------------------------------------------------------------------------
# Local stock library (reuse downloaded footage between runs)
# ----------------------------------------------------------------------------

_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_RU_TRANSLIT = str.maketrans(_RU2LAT)


def slugify(query: str) -> str:
    """
    'Snowy Pine Forest' -> 'snowy_pine_forest'. Cyrillic is transliterated
    ('лесная дорога' -> 'lesnaya_doroga') so new library folders - and
    therefore the media paths written into the fcpxml - stay pure ASCII:
    non-ASCII paths are a prime suspect when Resolve fails to find clips.
    """
    text = query.lower().translate(_RU_TRANSLIT)
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or "misc"


_MANIFEST_CACHE: dict = {}      # resolved folder path -> {file: pexels id}


def _load_manifest(folder: Path) -> dict:
    """{file name: pexels id} for a library folder (cached, best-effort)."""
    key = str(folder.resolve()).lower()
    if key not in _MANIFEST_CACHE:
        try:
            data = json.loads((folder / MANIFEST_NAME)
                              .read_text(encoding="utf-8"))
            _MANIFEST_CACHE[key] = dict(data) if isinstance(data, dict) \
                else {}
        except Exception:
            _MANIFEST_CACHE[key] = {}
    return _MANIFEST_CACHE[key]


def _save_manifest(folder: Path, manifest: dict):
    _MANIFEST_CACHE[str(folder.resolve()).lower()] = dict(manifest)
    try:
        (folder / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=1), encoding="utf-8")
    except Exception:
        pass    # the manifest is an optimization, never a hard failure


def stock_key(path: Path) -> str:
    """
    Canonical identity of a stock file for repeat counting: the Pexels id
    from the legacy pexels_<id> file name, else the id recorded in the
    folder's manifest.json (sequentially named files like Forest03.mp4),
    else the absolute path. Identity by id means the same stock never
    sneaks onto the timeline twice under different file names.
    Manifest entries are either a bare id (older) or {"id", "dur"}.
    """
    m = re.match(r"pexels_(\d+)$", path.stem)
    if m:
        return "pexels:" + m.group(1)
    entry = _load_manifest(path.parent).get(path.name)
    if isinstance(entry, dict):
        entry = entry.get("id")
    if entry is not None:
        return f"pexels:{entry}"
    return str(path.resolve()).lower()


_DURATION_CACHE: dict = {}


def _ffprobe_duration(path: Path) -> float | None:
    """
    Real media duration via ffprobe (ships with the ffmpeg install that
    Whisper already requires). Best-effort: None when unavailable.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def _stock_duration(path: Path) -> float | None:
    """
    Video duration in seconds. The ffprobe MEASUREMENT wins: Pexels'
    API "duration" is a rounded int that can overstate the real length,
    and cutting/declaring clips by it makes Resolve report insufficient
    media ("не хватает кадров"). The measurement is cached in memory
    and persisted into the manifest with a "probed" flag; the API hint
    is only a fallback when ffprobe is unavailable. None = unknown;
    callers treat unknown as "long enough".
    """
    if path.suffix.lower() in PHOTO_EXTS:
        return None
    key = str(path.resolve()).lower()
    if key in _DURATION_CACHE:
        return _DURATION_CACHE[key]
    manifest = _load_manifest(path.parent)
    entry = manifest.get(path.name)
    if not isinstance(entry, dict):
        entry = {"id": entry} if entry is not None else {}
    dur = entry.get("dur") if entry.get("probed") else None
    if dur is None:
        measured = _ffprobe_duration(path)
        if measured is not None:
            dur = measured
            entry["dur"] = round(measured, 3)
            entry["probed"] = True
            manifest[path.name] = entry
            _save_manifest(path.parent, manifest)
        else:
            dur = entry.get("dur")      # API hint - better than nothing
    _DURATION_CACHE[key] = dur
    return dur


class StockUsage:
    """
    Placement tracker enforcing the variety rules: one stock appears at
    most MAX_STOCK_USES times per timeline, and a repeat must be at
    least REPEAT_MIN_GAP other scenes away from its previous placement
    (no back-to-back or near repeats). fetch_stocks sets .pos to the
    scene number being filled before asking for candidates.
    """

    def __init__(self):
        self.counts: dict = {}      # stock_key -> placements so far
        self.last_pos: dict = {}    # stock_key -> scene number (1-based)
        self.pos = 0                # scene number being filled now

    def uses(self, key: str) -> int:
        return self.counts.get(key, 0)

    def usable(self, key: str) -> bool:
        if self.uses(key) >= MAX_STOCK_USES:
            return False
        last = self.last_pos.get(key)
        return last is None or (self.pos - last) > REPEAT_MIN_GAP

    def place(self, key: str):
        self.counts[key] = self.uses(key) + 1
        self.last_pos[key] = self.pos


def _norm_words(name: str) -> list:
    """Split a folder/query name into normalized words ('forests'->'forest')."""
    return [w.rstrip("s") for w in re.split(r"[_\s]+", name.lower()) if w]


def match_library_folder(library: Path, query: str) -> Path | None:
    """
    Find the library folder that best matches a search query. Rules:
      - exact word-set match always wins ('old diary' == 'diary old');
      - a MORE specific folder matches a broader query ('city' finds
        'city_night' - its footage IS the queried subject);
      - a LESS specific folder needs >= 2 shared words: a single-word
        folder like 'old' or 'man' must NOT swallow 'old diary' /
        'man chopping wood' - that put unrelated footage on scenes;
      - exception: a single-word folder equal to the query's HEAD (last)
        word matches ('diary' folder <- 'old diary'), because the head
        word IS the subject.
    The folder with the largest overlap wins. Empty folders still
    match - the stocking step will fill them.
    """
    if not library.exists():
        return None
    q_list = _norm_words(query)
    q_words = set(q_list)
    if not q_words:
        return None
    head = q_list[-1]
    best, best_score = None, 0
    for d in sorted(library.iterdir()):
        if not d.is_dir():
            continue
        f_words = set(_norm_words(d.name))
        if not f_words:
            continue
        if f_words == q_words:
            score = len(f_words) + 2
        elif q_words <= f_words:
            score = len(q_words)
        elif f_words <= q_words and len(f_words) >= 2:
            score = len(f_words)
        elif f_words == {head}:
            score = 1
        else:
            continue
        if score > best_score:
            best, best_score = d, score
    return best


def library_files(folder: Path, prefer: str = "video") -> list:
    """Usable files in a library folder, preferred media type first."""
    vids, phts = [], []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in VIDEO_EXTS:
            vids.append(f)
        elif ext in PHOTO_EXTS:
            phts.append(f)
    return (phts + vids) if prefer == "photo" else (vids + phts)


def pick_from_library(folder: Path, usage: StockUsage,
                      prefer: str = "video", need: float | None = None,
                      relax_gap: bool = False) -> Path | None:
    """
    Pick a library file for a scene honoring the variety rules: the
    MAX_STOCK_USES cap is always hard; the REPEAT_MIN_GAP distance is
    enforced too unless relax_gap is set (emergency for library-only
    runs with no Pexels keys, where the alternative is failing the
    scene). Ranking: less-used files win, then the preferred media
    type, then videos long enough to cover `need` seconds (short ones
    would have to be chained), then farther-away repeats; remaining
    ties are broken randomly for variety.
    """
    ranked = []
    for f in library_files(folder, prefer):
        key = stock_key(f)
        if relax_gap:
            if usage.uses(key) >= MAX_STOCK_USES:
                continue
        elif not usage.usable(key):
            continue
        is_pref = (f.suffix.lower() in PHOTO_EXTS) == (prefer == "photo")
        too_short = 0
        if need and f.suffix.lower() in VIDEO_EXTS:
            dur = _stock_duration(f)
            if dur is not None and dur < need - CHAIN_TOLERANCE:
                too_short = 1
        last = usage.last_pos.get(key)
        dist = usage.pos - last if last is not None else usage.pos + 10 ** 6
        ranked.append(((usage.uses(key), 0 if is_pref else 1,
                        too_short, -dist), f))
    if not ranked:
        return None
    best = min(rank for rank, _ in ranked)
    return random.choice([f for rank, f in ranked if rank == best])


# ----------------------------------------------------------------------------
# Step 4: Pexels client (key rotation) + stock selection
# ----------------------------------------------------------------------------

class PexelsKeysError(RuntimeError):
    """Every Pexels key is rejected/throttled - retrying is pointless."""


class PexelsClient:
    """
    Pexels API wrapper with primary/backup key rotation and an in-run
    search cache: repeated identical searches (common when neighboring
    scenes share a topic or fall back to the generic query) cost zero
    extra API calls, which matters for the hourly rate limit.
    """

    def __init__(self, keys: list, log, cancel_event=None):
        self.keys = [k for k in keys if k]
        if not self.keys:
            raise RuntimeError("At least one Pexels API key is required.")
        self.idx = 0
        self.log = log
        self.cancel_event = cancel_event
        self.session = requests.Session()
        self._cache: dict = {}      # (kind, query, page) -> result list

    def _get(self, url: str, params: dict) -> dict:
        last_status = None
        for _ in range(len(self.keys)):
            key = self.keys[self.idx]
            r = self.session.get(url, headers={"Authorization": key},
                                 params=params, timeout=30)
            if r.status_code in (401, 403, 429):
                last_status = r.status_code
                self.log(f"  ! Pexels key #{self.idx + 1} rejected "
                         f"(HTTP {r.status_code}), switching to backup key...")
                self.idx = (self.idx + 1) % len(self.keys)
                continue
            r.raise_for_status()
            return r.json()
        raise PexelsKeysError(
            f"All Pexels keys failed (last HTTP {last_status}). "
            "Check the keys or the rate limits.")

    @staticmethod
    def _params(query: str, extra: dict) -> dict:
        params = {"query": query, "orientation": "landscape", **extra}
        # queries slip through in Russian when the LLM is down and the
        # local keyword fallback kicks in; the locale hint makes Pexels
        # search Russian tags instead of returning near-random results
        if re.search(r"[А-Яа-яЁё]", query):
            params["locale"] = "ru-RU"
        return params

    def search_videos(self, query: str, page: int = 1) -> list:
        ck = ("video", query.lower(), page)
        if ck not in self._cache:
            data = self._get(PEXELS_VIDEO_SEARCH_URL, self._params(query, {
                "per_page": VIDEO_PER_PAGE, "page": page, "size": "medium",
            }))
            self._cache[ck] = data.get("videos", [])
        return self._cache[ck]

    def search_photos(self, query: str, page: int = 1) -> list:
        ck = ("photo", query.lower(), page)
        if ck not in self._cache:
            data = self._get(PEXELS_PHOTO_SEARCH_URL, self._params(query, {
                "per_page": PHOTO_PER_PAGE, "page": page,
            }))
            self._cache[ck] = data.get("photos", [])
        return self._cache[ck]

    def download(self, url: str, out_file: Path, retries: int = 2):
        """
        Atomic download: stream into a temporary .part file and rename it
        into place only on success. A crash, cancel or app close can never
        leave a truncated media file in the library.
        """
        tmp = out_file.with_suffix(out_file.suffix + ".part")
        for attempt in range(1, retries + 1):
            try:
                with self.session.get(url, stream=True, timeout=120) as dl:
                    dl.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in dl.iter_content(chunk_size=1024 * 256):
                            if (self.cancel_event is not None
                                    and self.cancel_event.is_set()):
                                f.close()
                                tmp.unlink(missing_ok=True)
                                raise CancelledError()
                            f.write(chunk)
                tmp.replace(out_file)
                return
            except CancelledError:
                raise
            except Exception as e:
                tmp.unlink(missing_ok=True)
                if attempt == retries:
                    raise
                self.log(f"  ! Download failed ({e}), retrying...")


def pick_video_file(video: dict) -> dict | None:
    """
    Pick the best 16:9 landscape file of a Pexels video.
    Priority: resolution closest to 1080p first, then ~60 fps softly
    preferred over lower frame rates. Minimum 720p (YouTube quality floor).
    Returns the chosen video_file dict or None.
    """
    best, best_score = None, None
    for vf in video.get("video_files", []):
        w, h = vf.get("width") or 0, vf.get("height") or 0
        fps = vf.get("fps") or 0
        if not w or not h or w <= h:
            continue                       # landscape only
        if abs(w / h - 16 / 9) > 0.05:
            continue                       # strict 16:9 for YouTube
        if h < 720:
            continue                       # at least HD
        # score: closest to 1080p wins; 60 fps is a soft bonus, not a filter
        score = (abs(h - 1080), 0 if fps >= 50 else 1, -fps)
        if best_score is None or score < best_score:
            best, best_score = vf, score
    return best


def scene_query_ladder(query: str) -> list:
    """
    Progressively broader queries that stay ON TOPIC: full query ->
    first two words -> head word (last: 'old diary' -> 'diary') ->
    first word -> generic. Single-word steps keep the scene close to
    its meaning before the last-chance generic query, instead of
    jumping straight to unrelated 'cinematic landscape' footage.
    """
    words = query.split()
    ladder = [query]
    if len(words) > 2:
        ladder.append(" ".join(words[:2]))
    if len(words) > 1:
        ladder.append(words[-1])
        ladder.append(words[0])
    ladder.append(GENERIC_QUERY)
    out, seen = [], set()
    for q in ladder:
        ql = q.lower()
        if len(ql) >= 3 and ql not in seen:
            seen.add(ql)
            out.append(q)
    return out


def _video_quality(vf: dict) -> tuple:
    """Lower is better: distance from 1080p, then a sub-50fps penalty."""
    return (abs((vf.get("height") or 0) - 1080),
            0 if (vf.get("fps") or 0) >= 50 else 1)


def _search_video(client: PexelsClient, scene: dict, need: float,
                  usage: StockUsage, log) -> tuple:
    """
    Find a video candidate respecting the repeat cap and the minimum
    repeat gap. Walks the query ladder and, when a page is exhausted
    (everything already used up / too recent / failing the 16:9-720p
    filters), fetches deeper result pages so long timelines keep getting
    fresh footage. Among equally good unused candidates the pick is
    randomized for variety.
    """
    for q in scene_query_ladder(scene["query"]):
        for page in range(1, MAX_SEARCH_PAGES + 1):
            try:
                videos = client.search_videos(q, page=page)
            except (CancelledError, PexelsKeysError):
                raise
            except Exception as e:
                log(f'  ! video search failed for "{q}": {e}')
                videos = []
            if not videos:
                break
            candidates = []
            for video in videos:
                key = f"pexels:{video['id']}"
                if not usage.usable(key):
                    continue
                vf = pick_video_file(video)
                if vf:
                    rank = (usage.uses(key),
                            video.get("duration", 0) < need)
                    candidates.append((rank, video, vf))
            if candidates:
                best = min(rank for rank, _, _ in candidates)
                pool = sorted((c for c in candidates if c[0] == best),
                              key=lambda c: _video_quality(c[2]))[:4]
                _, video, vf = random.choice(pool)
                if q != scene["query"]:
                    log(f'  broadened query to "{q}"')
                return video, vf
    return None, None


def _search_photo(client: PexelsClient, scene: dict,
                  usage: StockUsage, log):
    """Photo counterpart of _search_video (cap + gap + pagination)."""
    for q in scene_query_ladder(scene["query"]):
        for page in range(1, MAX_SEARCH_PAGES + 1):
            try:
                photos = client.search_photos(q, page=page)
            except (CancelledError, PexelsKeysError):
                raise
            except Exception as e:
                log(f'  ! photo search failed for "{q}": {e}')
                photos = []
            if not photos:
                break
            fresh = [(usage.uses(f"pexels:{p['id']}"), p)
                     for p in photos
                     if usage.usable(f"pexels:{p['id']}")]
            if fresh:
                best = min(uses for uses, _ in fresh)
                photo = random.choice([p for uses, p in fresh
                                       if uses == best])
                if q != scene["query"]:
                    log(f'  broadened query to "{q}"')
                return photo
    return None


def _download_new_stock(client: PexelsClient, scene: dict, target_dir: Path,
                        usage: StockUsage, log) -> Path:
    """
    Download one stock into the scene's library folder, honoring the
    scene's preferred media type ("video" / "photo") with cross-fallback:
    a photo-scene falls back to video and vice versa, so a scene is never
    left empty while Pexels has anything relevant. Stocks at the repeat
    cap or closer than REPEAT_MIN_GAP scenes to their previous use are
    never picked.
    """
    need = scene["end"] - scene["start"]
    prefer = scene.get("media", "video")
    order = ("photo", "video") if prefer == "photo" else ("video", "photo")

    for kind in order:
        if kind == "video":
            video, vf = _search_video(client, scene, need, usage, log)
            if vf:
                out_file = target_dir / f"pexels_{video['id']}.mp4"
                if out_file.exists():
                    log(f"  already in library: "
                        f"{target_dir.name}/{out_file.name}")
                else:
                    log(f"  video {vf['width']}x{vf['height']} "
                        f"@ {vf.get('fps') or '?'}fps "
                        f"-> {target_dir.name}/{out_file.name}")
                    client.download(vf["link"], out_file)
                scene["duration"] = float(video.get("duration", need))
                return out_file
            if prefer == "video":
                log("  ! no suitable video -> trying a photo instead")
        else:
            photo = _search_photo(client, scene, usage, log)
            if photo:
                url = _photo_crop_url(photo)
                out_file = target_dir / f"pexels_{photo['id']}.jpg"
                if out_file.exists():
                    log(f"  already in library: "
                        f"{target_dir.name}/{out_file.name}")
                else:
                    log(f"  photo 1920x1080 (16:9 crop) "
                        f"-> {target_dir.name}/{out_file.name}")
                    client.download(url, out_file)
                scene["duration"] = need
                return out_file
            if prefer == "photo":
                log("  ! no suitable photo -> trying a video instead")

    raise RuntimeError(
        f'No unused stock found for "{scene["query"]}" - Pexels returned '
        "nothing suitable (or the repeat cap exhausted every result).")


# ----------------------------------------------------------------------------
# Library folder pools (min 7 videos + 3 photos per theme, named Forest01...)
# ----------------------------------------------------------------------------

def _folder_media_counts(folder: Path) -> tuple:
    vids = phts = 0
    for f in folder.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in VIDEO_EXTS:
            vids += 1
        elif ext in PHOTO_EXTS:
            phts += 1
    return vids, phts


def folder_base_name(folder: Path) -> str:
    """'man_chopping_wood' -> 'ManChoppingWood' (stock file base name)."""
    return "".join(w.capitalize() for w in folder.name.split("_") if w) \
        or "Stock"


def folder_theme_query(folder: Path) -> str:
    """'man_chopping_wood' -> 'man chopping wood' (search query)."""
    return " ".join(w for w in folder.name.split("_") if w)


def _next_stock_number(folder: Path, base: str) -> int:
    pat = re.compile(re.escape(base) + r"(\d+)$", re.IGNORECASE)
    highest = 0
    for f in folder.iterdir():
        if f.is_file():
            m = pat.match(f.stem)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def _folder_ladder(theme: str) -> list:
    """
    Theme-confined ladder for stocking a folder: full theme -> first two
    words -> head word. NEVER broadens to the first modifier word or the
    generic query - everything downloaded into a folder must stay ON the
    folder's own topic, otherwise the pool poisons future timelines.
    """
    words = theme.split()
    ladder = [theme]
    if len(words) > 2:
        ladder.append(" ".join(words[:2]))
    if len(words) > 1:
        ladder.append(words[-1])
    out, seen = [], set()
    for q in ladder:
        ql = q.lower()
        if len(ql) >= 3 and ql not in seen:
            seen.add(ql)
            out.append(q)
    return out


def _photo_crop_url(photo: dict) -> str:
    return (f"{photo['src']['original']}?auto=compress&cs=tinysrgb"
            "&w=1920&h=1080&fit=crop")


def ensure_folder_stocked(client: PexelsClient, folder: Path, log,
                          cancel_event=None,
                          min_videos: int = FOLDER_MIN_VIDEOS,
                          min_photos: int = FOLDER_MIN_PHOTOS) -> int:
    """
    Fill a library folder up to min_videos videos + min_photos photos on
    the FOLDER's own theme (taken from its name), with sequential file
    names: Forest01.mp4 ... Forest08.jpg. manifest.json in the folder
    remembers Pexels ids, so the same stock is never downloaded into the
    folder twice (legacy pexels_<id> files are respected too). Downloads
    only the missing count; returns how many files were downloaded.
    """
    have_v, have_p = _folder_media_counts(folder)
    need_v = max(0, min_videos - have_v)
    need_p = max(0, min_photos - have_p)
    if not need_v and not need_p:
        return 0

    theme = folder_theme_query(folder)
    manifest = _load_manifest(folder)
    have_ids = set()
    for entry in manifest.values():
        have_ids.add(entry.get("id") if isinstance(entry, dict) else entry)
    for f in folder.iterdir():
        if f.is_file():
            m = re.match(r"pexels_(\d+)$", f.stem)
            if m:
                have_ids.add(int(m.group(1)))
    base = folder_base_name(folder)
    num = _next_stock_number(folder, base)
    log(f'  stocking "{folder.name}": +{need_v} video / +{need_p} photo')
    got = 0

    def grab(url: str, out_file: Path, item_id, dur=None) -> bool:
        nonlocal num, got
        _check_cancel(cancel_event)
        try:
            client.download(url, out_file)
        except (CancelledError, PexelsKeysError):
            raise
        except Exception as e:
            log(f"    ! download failed: {e}")
            return False
        manifest[out_file.name] = {"id": item_id, "dur": dur}
        have_ids.add(item_id)
        _save_manifest(folder, manifest)
        num += 1
        got += 1
        return True

    for q in _folder_ladder(theme):
        if need_v <= 0:
            break
        for page in range(1, MAX_SEARCH_PAGES + 1):
            if need_v <= 0:
                break
            try:
                videos = client.search_videos(q, page=page)
            except (CancelledError, PexelsKeysError):
                raise
            except Exception as e:
                log(f'  ! video search failed for "{q}": {e}')
                videos = []
            if not videos:
                break
            for video in videos:
                if need_v <= 0:
                    break
                if video["id"] in have_ids:
                    continue
                if (video.get("duration") or 0) < STOCK_MIN_DURATION:
                    continue    # too short to cover a typical scene
                vf = pick_video_file(video)
                if not vf:
                    continue
                out_file = folder / f"{base}{num:02d}.mp4"
                v_dur = float(video.get("duration") or 0) or None
                if grab(vf["link"], out_file, video["id"], v_dur):
                    need_v -= 1
                    log(f"    + {out_file.name} "
                        f"({vf['width']}x{vf['height']}, "
                        f"{v_dur or 0:.0f}s)")

    for q in _folder_ladder(theme):
        if need_p <= 0:
            break
        for page in range(1, MAX_SEARCH_PAGES + 1):
            if need_p <= 0:
                break
            try:
                photos = client.search_photos(q, page=page)
            except (CancelledError, PexelsKeysError):
                raise
            except Exception as e:
                log(f'  ! photo search failed for "{q}": {e}')
                photos = []
            if not photos:
                break
            for photo in photos:
                if need_p <= 0:
                    break
                if photo["id"] in have_ids:
                    continue
                out_file = folder / f"{base}{num:02d}.jpg"
                if grab(_photo_crop_url(photo), out_file, photo["id"]):
                    need_p -= 1
                    log(f"    + {out_file.name} (photo 1920x1080)")

    if need_v or need_p:
        log(f'  ! "{folder.name}" is still short by {need_v} video / '
            f'{need_p} photo - Pexels has no more suitable results')
    return got


def fetch_stocks(pexels_keys: list, scenes: list, library: Path,
                 stocks_dir: Path | None, log, progress_cb,
                 cancel_event=None):
    """
    LIBRARY-FIRST model. For every scene:
      1) match (or create) the keyword folder for the scene query;
      2) stock the folder up to FOLDER_MIN_VIDEOS videos +
         FOLDER_MIN_PHOTOS photos on ITS theme (sequential names like
         Forest01.mp4) - downloads happen only to fill/grow pools;
      3) pick from the pool honoring the repeat rules; if only repeats
         remain, the pool is grown by FOLDER_TOPUP before repeating;
      4) last resort: per-scene cross-media search (_download_new_stock).
    A single stock (identity via stock_key: Pexels id from the file name
    or the folder manifest) appears at most MAX_STOCK_USES times per
    timeline and never closer than REPEAT_MIN_GAP other scenes to its
    previous appearance; ties are randomized. The gap is relaxed (the cap
    never) only in library-only mode, where no key can bring anything new.
    If stocks_dir is given, used files are also copied there (one copy per
    unique file) so the project folder is self-contained.
    Adds to each scene: 'type' ("video" | "photo"), 'file', 'duration'.
    """
    library.mkdir(parents=True, exist_ok=True)

    _client: list = []

    def client() -> PexelsClient:
        if not _client:
            _client.append(PexelsClient(pexels_keys, log, cancel_event))
        return _client[0]

    usage = StockUsage()
    copied: dict = {}           # resolved src path -> copy in stocks_dir
    taken_names: set = set()
    reused = downloaded = 0
    has_keys = any(k for k in pexels_keys if k)

    idx = 0
    while idx < len(scenes):
        scene = scenes[idx]
        idx += 1
        _check_cancel(cancel_event)
        usage.pos = idx
        prefer = scene.get("media", "video")
        need = scene["end"] - scene["start"]
        log(f'[{idx}/{len(scenes)}] "{scene["query"]}" '
            f'({prefer}, {need:.1f}s)')

        folder = match_library_folder(library, scene["query"])
        if folder is None:
            folder = library / slugify(scene["query"])
            folder.mkdir(parents=True, exist_ok=True)
            log(f"  new library folder: {folder.name}")

        if has_keys:
            downloaded += ensure_folder_stocked(client(), folder, log,
                                                cancel_event)

        src = pick_from_library(folder, usage, prefer, need)
        is_repeat = src is not None and usage.uses(stock_key(src)) > 0
        if (src is None or is_repeat) and has_keys:
            # the pool ran dry (or only repeats remain): grow THIS folder
            # beyond the minimum instead of repeating early
            have_v, have_p = _folder_media_counts(folder)
            downloaded += ensure_folder_stocked(
                client(), folder, log, cancel_event,
                min_videos=have_v + (FOLDER_TOPUP if prefer == "video"
                                     else 0),
                min_photos=have_p + (FOLDER_TOPUP if prefer == "photo"
                                     else 0))
            fresh = pick_from_library(folder, usage, prefer, need)
            if fresh is not None and (src is None
                                      or not usage.uses(stock_key(fresh))):
                src = fresh

        per_scene_dl = False
        if src is None and not has_keys:
            src = pick_from_library(folder, usage, prefer, need,
                                    relax_gap=True)
            if src is not None:
                log("  ! repeat gap relaxed (library-only mode, nothing "
                    "fresh left for this query)")
        if src is None and has_keys:
            # absolute last resort: cross-media / broadened search
            src = _download_new_stock(client(), scene, folder, usage, log)
            downloaded += 1
            per_scene_dl = True
        if src is None:
            raise RuntimeError(
                f'No stock available for "{scene["query"]}" - the '
                "library pool is exhausted and no Pexels key can grow it.")

        scene["type"] = ("photo" if src.suffix.lower() in PHOTO_EXTS
                         else "video")
        if not per_scene_dl:
            reused += 1
        if scene["type"] == "video":
            # the measured duration wins even for per-scene downloads:
            # chaining by the rounded API int overruns the real media
            dur = _stock_duration(src)
            scene["duration"] = dur or scene.get("duration") or need
        else:
            scene["duration"] = need
        log(f"  -> {folder.name}/{src.name}")
        usage.place(stock_key(src))

        # the picked video cannot cover the whole scene: cut the scene at
        # the stock's end and chain a DIFFERENT stock (repeat rules make
        # back-to-back reuse of the same one impossible) for the rest
        avail = scene.get("duration") or need
        if (scene["type"] == "video" and need - avail > CHAIN_TOLERANCE
                and avail >= CHAIN_MIN_PIECE):
            split = scene["start"] + avail
            if scene["end"] - split < CHAIN_MIN_PIECE:
                split = scene["end"] - CHAIN_MIN_PIECE
            if split - scene["start"] >= CHAIN_MIN_PIECE:
                rest = {"start": round(split, 2), "end": scene["end"],
                        "query": scene["query"], "media": scene["media"],
                        "text": scene.get("text", "")}
                scene["end"] = rest["start"]
                scenes.insert(idx, rest)
                log(f"  stock covers {avail:.1f}s of {need:.1f}s - "
                    "chaining another stock for the rest")

        final = src
        if stocks_dir is not None:
            stocks_dir.mkdir(parents=True, exist_ok=True)
            skey = str(src.resolve()).lower()
            final = copied.get(skey)
            if final is None:
                name = src.name
                if name.lower() in taken_names:   # same name, other folder
                    name = f"{src.parent.name}_{name}"
                final = stocks_dir / name
                if not final.exists():
                    shutil.copy2(src, final)
                copied[skey] = final
                taken_names.add(name.lower())
        scene["file"] = final

        progress_cb(0.35 + 0.55 * (idx / len(scenes)))

    n_photo = sum(1 for s in scenes if s["type"] == "photo")
    log(f"Stocks: {reused} scenes served from the library pools, "
        f"{downloaded} new files downloaded into the library.")
    log(f"Media mix: {len(scenes) - n_photo} video / {n_photo} photo "
        f"({round(100 * n_photo / len(scenes))}% photo).")


# ----------------------------------------------------------------------------
# Step 5: FCPXML generation
# ----------------------------------------------------------------------------

def generate_fcpxml(scenes: list, audio_path: Path | None, total_dur: float,
                    out_path: Path, project_name: str, log):
    """
    Build .fcpxml v1.8: sequential clips/stills on the spine, the voiceover
    (if provided) attached on lane -1, frame-accurate at 60 fps.
    Stills use a rate-undefined format and <video> spine elements.
    Every unique media file becomes exactly ONE <asset> (a stock repeated
    on the timeline is referenced again, not re-declared with a duplicate
    src), and all files are verified to exist before writing - both keep
    Resolve's media linking reliable.
    """
    log("Generating FCPXML timeline...")

    total_frames = sec_to_frames(total_dur)

    # pass 1: unique assets, existence check, per-file max asset duration
    missing = []
    if audio_path and not audio_path.is_file():
        missing.append(str(audio_path))
    assets: dict = {}   # resolved path -> {"id", "path", "photo", "dur_f"}
    clips = []          # per scene: (asset info, start_f, dur_f, clip name)
    for i, scene in enumerate(scenes, 1):
        p = Path(scene["file"])
        key = str(p.resolve()).lower()
        info = assets.get(key)
        if info is None:
            if not p.is_file():
                missing.append(str(p))
            info = assets[key] = {
                "id": f"a{len(assets) + 1}", "path": p, "dur_f": 0,
                "photo": scene.get("type") == "photo",
            }
        start_f = sec_to_frames(scene["start"])
        end_f = (total_frames if i == len(scenes)   # snap last clip to end
                 else sec_to_frames(scene["end"]))
        dur_f = max(1, end_f - start_f)
        info["dur_f"] = max(info["dur_f"], dur_f,
                            sec_to_frames(scene.get("duration", 0)))
        clips.append((info, start_f, dur_f,
                      xml_escape(scene.get("query", f"scene {i}"))))

    if missing:
        raise RuntimeError(
            "These media files do not exist on disk (Resolve would show "
            "them offline):\n  " + "\n  ".join(missing))

    non_ascii = [str(info["path"]) for info in assets.values()
                 if not str(info["path"].resolve()).isascii()]
    if audio_path and not str(audio_path.resolve()).isascii():
        non_ascii.append(str(audio_path))
    if non_ascii:
        log(f"! WARNING: {len(non_ascii)} media path(s) contain non-ASCII "
            "characters (Cyrillic etc). If Resolve shows these clips "
            "offline, move the media to a plain-ASCII folder (or enable "
            "'Copy used stocks' with an ASCII output path) and regenerate.")

    resources = [
        f'    <format id="r1" name="FFVideoFormat1080p{FPS}" '
        f'frameDuration="1/{FPS}s" width="1920" height="1080"/>'
    ]
    if any(info["photo"] for info in assets.values()):
        resources.append(
            '    <format id="r2" name="FFVideoFormatRateUndefined" '
            'width="1920" height="1080"/>'
        )
    if audio_path:
        audio_uri = xml_escape(file_src(audio_path))
        resources.append(
            f'    <asset id="a_audio" name="{xml_escape(audio_path.stem)}" '
            f'src="{audio_uri}" start="0/{FPS}s" '
            f'duration="{frames_to_rational(total_frames)}" '
            f'hasAudio="1" audioSources="1" audioChannels="2"/>'
        )
    for info in assets.values():
        uri = xml_escape(file_src(info["path"]))
        if info["photo"]:
            resources.append(
                f'    <asset id="{info["id"]}" '
                f'name="{xml_escape(info["path"].stem)}" '
                f'src="{uri}" start="0/{FPS}s" duration="0s" '
                f'hasVideo="1" format="r2"/>'
            )
        else:
            resources.append(
                f'    <asset id="{info["id"]}" '
                f'name="{xml_escape(info["path"].stem)}" '
                f'src="{uri}" start="0/{FPS}s" '
                f'duration="{frames_to_rational(info["dur_f"])}" '
                f'hasVideo="1" format="r1"/>'
            )

    spine_items = []
    for n, (info, start_f, dur_f, name) in enumerate(clips, 1):
        inner = ""
        if n == 1 and audio_path:
            inner = (
                f'\n            <asset-clip ref="a_audio" lane="-1" '
                f'offset="0/{FPS}s" start="0/{FPS}s" '
                f'duration="{frames_to_rational(total_frames)}" '
                f'name="{xml_escape(audio_path.stem)}"/>\n          '
            )

        if info["photo"]:
            spine_items.append(
                f'          <video ref="{info["id"]}" '
                f'offset="{frames_to_rational(start_f)}" start="0/{FPS}s" '
                f'duration="{frames_to_rational(dur_f)}" '
                f'name="{name}">{inner}</video>'
            )
        else:
            spine_items.append(
                f'          <asset-clip ref="{info["id"]}" '
                f'offset="{frames_to_rational(start_f)}" start="0/{FPS}s" '
                f'duration="{frames_to_rational(dur_f)}" '
                f'name="{name}" format="r1">{inner}</asset-clip>'
            )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.8">\n'
        "  <resources>\n"
        + "\n".join(resources)
        + "\n  </resources>\n"
        "  <library>\n"
        '    <event name="AutoStock">\n'
        f'      <project name="{xml_escape(project_name)}">\n'
        f'        <sequence format="r1" tcStart="0/{FPS}s" tcFormat="NDF" '
        f'audioLayout="stereo" audioRate="48k" '
        f'duration="{frames_to_rational(total_frames)}">\n'
        "        <spine>\n"
        + "\n".join(spine_items)
        + "\n        </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )
    out_path.write_text(xml, encoding="utf-8")
    log(f"FCPXML saved: {out_path}")


# ----------------------------------------------------------------------------
# Full pipeline (runs in a worker thread)
# ----------------------------------------------------------------------------

def run_pipeline(cfg: dict, log, progress_cb, done_cb, cancel_event=None):
    try:
        out_dir = Path(cfg["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        library = Path(cfg.get("library_dir") or DEFAULT_LIBRARY)
        stocks_dir = (out_dir / "stock_media") if cfg.get("copy_stocks") \
            else None

        audio = cfg.get("audio") or None
        script = cfg.get("script") or None
        script_text = ""
        if script:
            script_text = read_text_best_effort(Path(script)).strip()

        # 1. Timing ------------------------------------------------------------
        progress_cb(0.02)
        _check_cancel(cancel_event)
        if audio:
            segments = transcribe_audio(audio, log)
            total_dur = segments[-1]["end"]
        else:
            segments, total_dur = estimate_segments_from_text(script_text, log)
        progress_cb(0.25)
        _check_cancel(cancel_event)

        # 2. Narration units ---------------------------------------------------
        units = build_units(segments, log)

        # 3. Queries per unit -> topic-merged scenes ---------------------------
        build_queries([cfg.get("openrouter_key", ""),
                       cfg.get("openrouter_key2", "")],
                      units, segments, log)
        scenes = merge_units_into_scenes(units, total_dur, log)
        progress_cb(0.35)
        _check_cancel(cancel_event)

        # 4. Find / download stocks (library first) ---------------------------
        pexels_keys = list(cfg.get("pexels_keys") or
                           [cfg.get("pexels_key", ""),
                            cfg.get("pexels_key2", "")])
        fetch_stocks(pexels_keys, scenes, library, stocks_dir, log,
                     progress_cb, cancel_event)
        progress_cb(0.92)
        _check_cancel(cancel_event)

        # 5. FCPXML ------------------------------------------------------------
        base_name = Path(audio).stem if audio else Path(script).stem
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", base_name).strip() \
            or "project"
        audio_src = Path(audio) if audio else None
        if audio_src and stocks_dir is not None:
            # keep the whole project (stocks + voiceover) in one folder
            # with an ASCII name - simplifies Resolve linking/relinking
            stocks_dir.mkdir(parents=True, exist_ok=True)
            voice_copy = stocks_dir / f"voiceover{audio_src.suffix.lower()}"
            if audio_src.resolve() != voice_copy.resolve():
                shutil.copy2(audio_src, voice_copy)
                log(f"Voiceover copied into the project: {voice_copy.name}")
            audio_src = voice_copy
        xml_path = out_dir / f"{safe_name}_davinci.fcpxml"
        generate_fcpxml(scenes, audio_src, total_dur,
                        xml_path, project_name=safe_name, log=log)
        progress_cb(1.0)

        log("=" * 50)
        log("DONE! Import into DaVinci Resolve:")
        log("File -> Import Timeline -> Import AAF, EDL, XML, DRT, OTIO...")
        log("(do NOT use 'Import Project' - that one only accepts .drp files)")
        log("If Resolve still reports missing clips: first drag the used "
            "media folders (stock library / stock_media) into the Media "
            "Pool, then repeat File -> Import Timeline - Resolve will "
            "relink by file name.")
        done_cb(True)
    except CancelledError:
        log("=" * 50)
        log("CANCELLED. Already-downloaded stocks remain in the library "
            "and will be reused next time.")
        done_cb(False)
    except Exception:
        log("ERROR:\n" + traceback.format_exc())
        done_cb(False)


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def _gradient_color(stops: list, t: float) -> tuple:
    """Interpolate an (r, g, b) color at position t within multi-stop stops."""
    t = min(1.0, max(0.0, t))
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
    return stops[-1][1]


def _hex(rgb: tuple) -> str:
    return "#%02x%02x%02x" % rgb


def _round_rect(canvas: "tk.Canvas", x0, y0, x1, y1, r, fill):
    """Rounded rectangle as a smooth polygon."""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, fill=fill, outline="")


class GradientButton(tk.Canvas):
    """Rounded button with a horizontal color gradient (pure tkinter)."""

    def __init__(self, parent, text: str, command, stops=None, height=50,
                 radius=16, font=(FONT_HEAD, 15), text_color="#ffffff",
                 bg=COL_BG):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0,
                         cursor="hand2")
        self._text = text
        self._command = command
        self._stops = stops or GRAD_BTN
        self._radius = radius
        self._font = font
        self._text_color = text_color
        self._bgcol = bg
        self._state = "normal"
        self._hover = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    def configure_state(self, state: str):
        self._state = state
        self.configure(cursor="hand2" if state == "normal" else "arrow")
        self._draw()

    def set_text(self, text: str):
        self._text = text
        self._draw()

    def _set_hover(self, value: bool):
        self._hover = value
        self._draw()

    def _on_click(self, _event):
        if self._state == "normal" and self._command:
            self._command()

    def _color_at(self, t: float) -> str:
        rgb = _gradient_color(self._stops, t)
        if self._state == "disabled":
            rgb = tuple(int(c * 0.35 + 80 * 0.65) for c in rgb)  # dim to gray
        elif self._hover:
            rgb = tuple(min(255, int(c * 1.12)) for c in rgb)
        return _hex(rgb)

    def _draw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        r = min(self._radius, h // 2)
        steps = max(2, w // 2)
        for i in range(steps):
            t = i / steps
            self.create_rectangle(w * t, 0, w * (t + 1.0 / steps) + 1, h,
                                  fill=self._color_at(t), width=0)
        # carve square corners back to the window bg...
        b = self._bgcol
        self.create_rectangle(0, 0, r, r, fill=b, width=0)
        self.create_rectangle(w - r, 0, w, r, fill=b, width=0)
        self.create_rectangle(0, h - r, r, h, fill=b, width=0)
        self.create_rectangle(w - r, h - r, w, h, fill=b, width=0)
        # ...and repaint them as quarter-circles in the local gradient color
        self.create_arc(0, 0, 2 * r, 2 * r, start=90, extent=90,
                        fill=self._color_at(0), outline="", style="pieslice")
        self.create_arc(w - 2 * r, 0, w, 2 * r, start=0, extent=90,
                        fill=self._color_at(1), outline="", style="pieslice")
        self.create_arc(0, h - 2 * r, 2 * r, h, start=180, extent=90,
                        fill=self._color_at(0), outline="", style="pieslice")
        self.create_arc(w - 2 * r, h - 2 * r, w, h, start=270, extent=90,
                        fill=self._color_at(1), outline="", style="pieslice")
        self.create_text(w / 2, h / 2, text=self._text, font=self._font,
                         fill=self._text_color if self._state == "normal"
                         else "#9a9ab0")


class GradientProgress(tk.Canvas):
    """Slim rounded progress bar whose fill reveals a neon gradient."""

    def __init__(self, parent, stops=None, height=10, bg=COL_BG,
                 track=COL_FIELD):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0)
        self._stops = stops or GRAD_MAIN
        self._track = track
        self._value = 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, value: float):
        self._value = min(1.0, max(0.0, value))
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(4, self.winfo_width())
        h = max(6, self.winfo_height())
        r = h / 2
        _round_rect(self, 0, 0, w, h, r, self._track)
        fw = int(w * self._value)
        if fw < 2:
            return
        fw = max(fw, h)  # keep a pill shape even at tiny values
        color = lambda x: _hex(_gradient_color(self._stops, x / w))
        self.create_arc(0, 0, h, h, start=90, extent=180,
                        fill=color(0), outline="", style="pieslice")
        x = r
        while x < fw - r:
            nx = min(x + 3, fw - r)
            self.create_rectangle(x, 0, nx, h, fill=color(x), width=0)
            x = nx
        self.create_arc(fw - h, 0, fw, h, start=-90, extent=180,
                        fill=color(fw - 1), outline="", style="pieslice")


class App(ctk.CTk):
    N_PEXELS = 5    # exactly five Pexels key slots

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x860")
        self.minsize(800, 720)
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=COL_BG)
        try:
            ico = app_dir() / "icon.ico"
            if not ico.exists():
                ico = Path(__file__).with_name("icon.ico")
            if platform.system() == "Windows" and ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass

        self.audio_path = ctk.StringVar()
        self.script_path = ctk.StringVar()
        self.out_dir = ctk.StringVar()
        self.library_dir = ctk.StringVar(value=str(DEFAULT_LIBRARY))
        self.copy_stocks = tk.BooleanVar(value=False)
        self.cancel_event = threading.Event()
        # single thread-safe channel for ALL worker -> GUI updates;
        # the worker thread must never touch tkinter directly
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._enable_clipboard_support()
        self._load_saved_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_ui_queue)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        pad = 18

        # --- Header ---
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=pad, pady=(16, 10))
        ctk.CTkLabel(head, text="◆", font=(FONT_HEAD, 20),
                     text_color=ACC_PINK).pack(side="left", padx=(2, 10))
        ctk.CTkLabel(head, text="AutoStock Editor", font=(FONT_HEAD, 22),
                     text_color=COL_TEXT).pack(side="left")
        ctk.CTkLabel(head, text="for DaVinci Resolve", font=(FONT_UI, 13),
                     text_color=COL_MUTED).pack(side="left", padx=10,
                                                pady=(5, 0))
        ctk.CTkLabel(head, text="🎬", font=(FONT_UI, 14), width=36, height=30,
                     fg_color=COL_FIELD, corner_radius=8
                     ).pack(side="right")

        # --- Body grid: 2 columns ---
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        body.grid_columnconfigure((0, 1), weight=1, uniform="col")
        body.grid_rowconfigure(2, weight=1)

        # --- API keys card (left) ---
        keys = self._card(body, "API KEYS", "🔑")
        keys.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
        self.openrouter_entry = self._entry(keys, "OpenRouter (Primary)")
        self.openrouter_entry2 = self._entry(keys, "OpenRouter (Backup)")
        self.pexels_entries = []
        for i in range(self.N_PEXELS):
            name = "Pexels (Primary)" if i == 0 else f"Pexels (Backup {i})"
            e = self._entry(keys, name, last=(i == self.N_PEXELS - 1))
            self.pexels_entries.append(e)

        # --- Inputs card (right) ---
        inputs = self._card(body, "INPUTS", "🎞")
        inputs.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 12))
        self._pick_row(inputs, "🎵", "Audio (.mp3 / .wav)", self.audio_path,
                       self.pick_audio)
        self._pick_row(inputs, "📄", "Script (.txt)", self.script_path,
                       self.pick_script)
        ctk.CTkLabel(inputs,
                     text="Audio or script is enough — provide either one\n"
                          "(or both for best results).",
                     font=(FONT_UI, 11), text_color=COL_MUTED,
                     justify="left").pack(anchor="w", padx=16, pady=(4, 14))

        # --- Outputs & options card (full width) ---
        outs = self._card(body, "OUTPUTS & OPTIONS", "📦")
        outs.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        self._pick_row(outs, "📁", "Output folder", self.out_dir,
                       self.pick_out_dir, show_full=True)
        self._pick_row(outs, "📚", "Stock library", self.library_dir,
                       self.pick_library_dir, show_full=True)
        ctk.CTkSwitch(outs, text="Copy used stocks into the project folder",
                      variable=self.copy_stocks,
                      font=(FONT_UI, 11), text_color=COL_MUTED,
                      progress_color=ACC_VIOLET, button_color=COL_TEXT,
                      fg_color=COL_FIELD).pack(anchor="w", padx=16,
                                               pady=(6, 2))
        ctk.CTkLabel(outs,
                     text="The library keeps every downloaded stock in "
                          "keyword folders and reuses them next time.",
                     font=(FONT_UI, 11), text_color=COL_MUTED,
                     wraplength=760, justify="left"
                     ).pack(anchor="w", padx=16, pady=(0, 14))

        # --- Actions & terminal card (full width, grows) ---
        act = self._card(body, "ACTIONS & TERMINAL", "⚡")
        act.grid(row=2, column=0, columnspan=2, sticky="nsew")

        btn_row = ctk.CTkFrame(act, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(2, 8))
        self.run_btn = GradientButton(
            btn_row, "Generate DaVinci Project", self.start,
            height=50, font=(FONT_HEAD, 15), bg=COL_CARD)
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ctk.CTkButton(
            btn_row, text="Cancel", width=118, height=46,
            font=(FONT_HEAD, 13), state="disabled",
            fg_color=COL_FIELD, hover_color="#c2436f",
            text_color=COL_TEXT, corner_radius=14,
            command=self.cancel)
        self.cancel_btn.pack(side="left", padx=(10, 0), pady=2)

        self.progress = GradientProgress(act, height=8, bg=COL_CARD,
                                         track=COL_FIELD)
        self.progress.pack(fill="x", padx=16, pady=(0, 8))

        self.log_box = ctk.CTkTextbox(
            act, font=(FONT_MONO, 12), fg_color=COL_LOG_BG,
            text_color="#c9cbe0", corner_radius=12,
            border_width=1, border_color=COL_BORDER)
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")

        self.open_btn = ctk.CTkButton(
            self, text="Open Output Folder", state="disabled", height=36,
            font=(FONT_UI, 13),
            fg_color=COL_FIELD, hover_color=COL_HOVER,
            text_color=COL_TEXT, corner_radius=12,
            command=lambda: open_folder(self.out_dir.get()))
        self.open_btn.pack(fill="x", padx=pad, pady=(0, 16))

    def _card(self, parent, title: str, icon: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=COL_CARD, corner_radius=16)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(head, text=icon, font=(FONT_UI, 14),
                     text_color=COL_MUTED).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(head, text=title, font=(FONT_HEAD, 13),
                     text_color=COL_TEXT).pack(side="left")
        return card

    def _entry(self, parent, placeholder: str, last: bool = False):
        e = ctk.CTkEntry(
            parent, placeholder_text=placeholder, show="*", height=34,
            font=(FONT_UI, 12),
            fg_color=COL_FIELD, border_color=COL_BORDER, border_width=1,
            text_color=COL_TEXT, placeholder_text_color=PLACEHOLDER_PINK,
            corner_radius=10)
        e.pack(fill="x", padx=16, pady=(3, 14 if last else 3))
        return e

    def _pick_row(self, parent, icon: str, placeholder: str, var,
                  pick_cmd, show_full: bool = False):
        """Entry-look pill row: click to pick, ✕ to clear."""
        row = ctk.CTkFrame(parent, fg_color=COL_FIELD, corner_radius=10)
        row.pack(fill="x", padx=16, pady=4)
        lbl = ctk.CTkButton(
            row, text=f"{icon}  {placeholder}", anchor="w",
            fg_color="transparent", hover_color=COL_HOVER,
            text_color=COL_MUTED, font=(FONT_UI, 12),
            corner_radius=8, height=34, command=pick_cmd)
        lbl.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=2)
        ctk.CTkButton(row, text="✕", width=30, height=28,
                      font=(FONT_UI, 12),
                      fg_color="transparent", hover_color=COL_HOVER,
                      text_color=COL_MUTED, corner_radius=8,
                      command=lambda: var.set("")).pack(side="right",
                                                        padx=4, pady=2)

        def refresh(*_):
            value = var.get()
            if value:
                shown = value if show_full else Path(value).name
                lbl.configure(text=f"{icon}  {shown}", text_color=COL_TEXT)
            else:
                lbl.configure(text=f"{icon}  {placeholder}",
                              text_color=COL_MUTED)
        var.trace_add("write", refresh)
        refresh()

    # ---------------------------------------------------------- clipboard --
    def _enable_clipboard_support(self):
        """
        Layout-independent Ctrl+V/C/X/A (works with Russian layout) and a
        right-click Paste/Copy/Cut menu on all entry fields.
        """
        def on_key(event):
            if not (event.state & 0x4):          # Control not pressed
                return None
            key = (event.keysym or "").lower()
            code = event.keycode
            if code == 86 or key in ("v", "м"):
                event.widget.event_generate("<<Paste>>")
                return "break"
            if code == 67 or key in ("c", "с"):
                event.widget.event_generate("<<Copy>>")
                return "break"
            if code == 88 or key in ("x", "ч"):
                event.widget.event_generate("<<Cut>>")
                return "break"
            if code == 65 or key in ("a", "ф"):
                event.widget.event_generate("<<SelectAll>>")
                return "break"
            return None

        self.bind_all("<KeyPress>", on_key)

        menu = tk.Menu(self, tearoff=0)
        self._ctx_widget = None
        for lbl, ev in (("Вставить", "<<Paste>>"),
                        ("Копировать", "<<Copy>>"),
                        ("Вырезать", "<<Cut>>")):
            menu.add_command(
                label=lbl,
                command=lambda e=ev: self._ctx_widget.event_generate(e))
        menu.add_separator()
        menu.add_command(
            label="Выделить всё",
            command=lambda: self._ctx_widget.event_generate("<<SelectAll>>"))

        def show_menu(event):
            self._ctx_widget = event.widget
            event.widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        for entry in (self.openrouter_entry, self.openrouter_entry2,
                      *self.pexels_entries):
            target = getattr(entry, "_entry", entry)
            target.bind("<Button-3>", show_menu)
            target.bind("<Button-2>", show_menu)

    # ------------------------------------------------------------- config --
    def _collect_cfg(self) -> dict:
        return {
            "openrouter_key": self.openrouter_entry.get().strip(),
            "openrouter_key2": self.openrouter_entry2.get().strip(),
            "pexels_keys": [e.get().strip() for e in self.pexels_entries],
            "audio": self.audio_path.get().strip(),
            "script": self.script_path.get().strip(),
            "out_dir": self.out_dir.get().strip(),
            "library_dir": self.library_dir.get().strip(),
            "copy_stocks": self.copy_stocks.get(),
        }

    def _load_saved_config(self):
        cfg = load_config()
        if cfg.get("openrouter_key"):
            self.openrouter_entry.insert(0, cfg["openrouter_key"])
        if cfg.get("openrouter_key2"):
            self.openrouter_entry2.insert(0, cfg["openrouter_key2"])
        for entry, key in zip(self.pexels_entries,
                              cfg.get("pexels_keys") or []):
            if key:
                entry.insert(0, key)
        if cfg.get("out_dir"):
            self.out_dir.set(cfg["out_dir"])
        lib_cfg = cfg.get("library_dir") or ""
        if lib_cfg:
            lp = Path(lib_cfg)
            # migrate a stale path pointing into a removed _internal folder
            if "_internal" in lp.parts and not lp.exists():
                lib_cfg = str(DEFAULT_LIBRARY)
            self.library_dir.set(lib_cfg)
        self.copy_stocks.set(bool(cfg.get("copy_stocks", False)))

    def _save_current_config(self, cfg: dict):
        save_config({k: cfg.get(k, "") for k in
                     ("openrouter_key", "openrouter_key2", "pexels_keys",
                      "out_dir", "library_dir", "copy_stocks")})

    def _on_close(self):
        """Remember everything (keys, folders) on window close."""
        try:
            self._save_current_config(self._collect_cfg())
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------ pickers --
    def pick_audio(self):
        p = filedialog.askopenfilename(
            filetypes=[("Audio", "*.mp3 *.wav"), ("All files", "*.*")])
        if p:
            self.audio_path.set(p)

    def pick_script(self):
        p = filedialog.askopenfilename(
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if p:
            self.script_path.set(p)

    def pick_out_dir(self):
        p = filedialog.askdirectory()
        if p:
            self.out_dir.set(p)

    def pick_library_dir(self):
        p = filedialog.askdirectory()
        if p:
            self.library_dir.set(p)

    # ------------------------------------- worker -> GUI (thread-safe) --
    def log(self, msg: str):
        self.ui_queue.put(("log", msg))

    def set_progress(self, value: float):
        self.ui_queue.put(("progress", value))

    def _on_done(self, success: bool):
        self.ui_queue.put(("done", success))

    def _drain_ui_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "progress":
                    self.progress.set(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    # ---------------------------------------------------------------- run --
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        cfg = self._collect_cfg()

        lib = Path(cfg["library_dir"] or DEFAULT_LIBRARY)
        lib_has_files = lib.exists() and any(
            f.is_file() and f.suffix.lower() in (VIDEO_EXTS | PHOTO_EXTS)
            for f in lib.rglob("*"))

        problems = []
        if not any(cfg["pexels_keys"]) and not lib_has_files:
            problems.append("add a Pexels API key or put some footage "
                            "into the stock library")
        if not (cfg["audio"] or cfg["script"]):
            problems.append("select an audio file OR a script file")
        if cfg["audio"] and not Path(cfg["audio"]).is_file():
            problems.append("the audio file does not exist anymore")
        if cfg["script"] and not Path(cfg["script"]).is_file():
            problems.append("the script file does not exist anymore")
        if not cfg["out_dir"]:
            problems.append("select the output folder")
        if problems:
            self.log("! " + "; ".join(problems))
            return
        if not cfg["openrouter_key"] and not cfg["openrouter_key2"]:
            self.log("Note: no OpenRouter key - queries will be built "
                     "locally from keywords (lower relevance).")
        if not any(cfg["pexels_keys"]):
            self.log("Note: no Pexels key - only the local stock library "
                     "will be used.")

        self._save_current_config(cfg)
        self.cancel_event.clear()
        self.run_btn.configure_state("disabled")
        self.run_btn.set_text("Working...")
        self.cancel_btn.configure(state="normal", fg_color=ACC_PINK,
                                  text_color="#141414")
        self.open_btn.configure(state="disabled")
        self.progress.set(0)
        self.log("Starting pipeline...")

        self.worker = threading.Thread(
            target=run_pipeline,
            args=(cfg, self.log, self.set_progress, self._on_done,
                  self.cancel_event),
            daemon=True)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_btn.configure(state="disabled")
            self.log("Cancelling... will stop after the current operation "
                     "(transcription cannot be interrupted mid-way).")

    def _finish(self, success: bool):
        """Runs on the main thread via the UI queue."""
        self.run_btn.configure_state("normal")
        self.run_btn.set_text("Generate DaVinci Project")
        self.cancel_btn.configure(state="disabled", fg_color=COL_FIELD,
                                  text_color=COL_TEXT)
        if success:
            self.open_btn.configure(state="normal")


if __name__ == "__main__":
    App().mainloop()
