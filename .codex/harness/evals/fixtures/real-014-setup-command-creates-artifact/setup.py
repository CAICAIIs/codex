from pathlib import Path
import sys


if "--prepare" in sys.argv:
    Path("input.txt").write_text("prepared\n", encoding="utf-8")
elif "--artifact" in sys.argv:
    pass
