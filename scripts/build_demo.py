"""Assemble the static demo site from the web interface and a recorded snapshot.

    python scripts/build_demo.py <output-dir> [--snapshot demo/snapshot.json]

The site is the real web interface (app/web) plus data/snapshot.json, with window.HEA_DEMO set so every read is
answered from the snapshot instead of an API. It needs no server, so it can be hosted on GitHub Pages.
The snapshot itself is recorded from a running system with scripts/record_snapshot.js.
"""
import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEEP_JOBS = 5  # recent analyses are enough for a demo; benchmark runs would only add noise


def trim_jobs(snapshot: dict) -> None:
    """Keep the newest few jobs in every job listing and drop the pagination cursor."""
    for key, body in snapshot.items():
        if key.startswith("/jobs?") and isinstance(body, dict) and isinstance(body.get("data"), list):
            body["data"] = body["data"][:KEEP_JOBS]
            body["next_cursor"] = None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--snapshot", default=str(ROOT / "demo" / "snapshot.json"))
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "app" / "web", out, ignore=shutil.ignore_patterns("dashboard"))

    index = out / "index.html"
    html = index.read_text(encoding="utf-8")
    marker = '<script type="module" src="js/main.js"></script>'
    assert marker in html, "index.html changed: update the marker in build_demo.py"
    index.write_text(html.replace(marker, "<script>window.HEA_DEMO = true;</script>\n  " + marker), encoding="utf-8")

    snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    trim_jobs(snapshot)
    (out / "data").mkdir()
    (out / "data" / "snapshot.json").write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    size = (out / "data" / "snapshot.json").stat().st_size
    print(f"built {out} ({len(snapshot)} recorded responses, snapshot {size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
