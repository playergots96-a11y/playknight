"""
Stock library folder creator for AutoStock Editor.

Creates keyword folders inside ./stock_library (next to this script).
Drop your own 16:9 videos/photos into these folders - the main app will
reuse them instead of downloading from Pexels.

Usage:
    python create_library_folders.py                 # interactive
    python create_library_folders.py --starter       # popular starter set
    python create_library_folders.py forest, city, night sky
"""

import re
import sys
from pathlib import Path

LIBRARY = Path(__file__).with_name("stock_library")

STARTER_SET = [
    "forest", "city", "night city", "ocean", "sky", "clouds", "mountains",
    "nature", "people walking", "crowd", "technology", "computer", "office",
    "business meeting", "money", "road", "car driving", "rain", "sunset",
    "space", "fire", "water", "abstract",
]


def slugify(name: str) -> str:
    """'Night City' -> 'night_city' (safe folder name)."""
    slug = re.sub(r"[^a-z0-9а-яё]+", "_", name.lower()).strip("_")
    return slug or "misc"


def main():
    args = [a for a in sys.argv[1:] if a != "--starter"]
    names: list[str] = []

    if "--starter" in sys.argv:
        names += STARTER_SET
    if args:
        # allow both space- and comma-separated arguments
        joined = " ".join(args)
        names += [w.strip() for w in joined.split(",") if w.strip()]
    if not names:
        raw = input("Folder names, comma separated "
                    "(Enter = popular starter set): ").strip()
        names = ([w.strip() for w in raw.split(",") if w.strip()]
                 or STARTER_SET)

    LIBRARY.mkdir(exist_ok=True)
    created = existed = 0
    for name in names:
        folder = LIBRARY / slugify(name)
        if folder.exists():
            existed += 1
        else:
            folder.mkdir()
            created += 1
            print(f"  + {folder.name}")
    print(f"\nDone: {created} created, {existed} already existed.")
    print(f"Library: {LIBRARY}")
    print("Drop your own 16:9 clips/photos into these folders - "
          "the app will use them first.")


if __name__ == "__main__":
    main()
