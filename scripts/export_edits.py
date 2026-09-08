"""Download every generated span as a before/after pair, ready to edit.

What the console shows as a change record is two stills and a clip. What an
editor actually wants is the two SECONDS, side by side, as files: the span
as it was delivered, and the span as Gemini Omni or Veo re-rendered it. So
this writes one pair per edit into a folder, named for the film, the market
and the rule that asked for it.

The "after" is the model's own output, copied straight out of the state
bucket -- nothing is re-encoded, so what lands on disk is the file the
verifier signed off. The "before" is cut from the run's original master
over HTTP with ffmpeg, using the finding's own timecodes, which is why it
is re-encoded: a stream copy would snap to the nearest keyframe and stop
being the same span.

    python scripts/export_edits.py                     # ~/Desktop/customs-edits
    python scripts/export_edits.py --out /tmp/edits
    python scripts/export_edits.py --method omni       # or bridge
    python scripts/export_edits.py --dry-run

Reads the live store, so it needs a copy of customs.db from the state
bucket (--db, or gcloud storage cp it and pass the path).
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BUCKET = os.environ.get("STATE_BUCKET", "veoapi-469317-customs-state")
BASE = os.environ.get("CUSTOMS_URL",
                      "https://customs-app-akap4ao72a-ew.a.run.app")
# bridge is Veo generating the motion between two edited frames; omni is
# Gemini Omni rewriting the span in place. Those are the only two methods
# that produce a clip of their own -- a patch or a relight writes frames.
CLIP = re.compile(r"^gs://[^/]+/(run_[0-9a-f]+)/changes/"
                  r"(chg_[0-9a-f]+)_(bridge|omni)\.mp4$")


def sh(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:48] or "x"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "customs-edits"))
    ap.add_argument("--db", default="", help="a copy of the live customs.db")
    ap.add_argument("--method", choices=("omni", "bridge"), default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.db:
        os.environ["CUSTOMS_DB"] = args.db
    from customs.config import settings          # noqa: E402 -- after CUSTOMS_DB
    from customs.store import Store              # noqa: E402

    db = Store(settings.db_path)
    runs = {run.id: run for run in db.recent_runs(500)}
    # A change can name a finding that lives on another run of the same
    # film -- a fix carried over from the market that first paid for it --
    # so the lookup is across the store, not within the run. Without this
    # those edits came out with no market, no rule and no timecodes, which
    # also meant no before-span to cut.
    findings = {f.id: f for run_id in runs for f in db.findings(run_id)}
    if not runs:
        print(f"no runs in {settings.db_path}: pass --db with a copy of the "
              f"live store (gcloud storage cp gs://{BUCKET}/customs.db .)")
        return 1

    listing = sh("gcloud", "storage", "ls",
                 f"gs://{BUCKET}/**/changes/*.mp4").stdout.splitlines()
    clips = []
    for line in listing:
        match = CLIP.match(line.strip())
        if match:
            clips.append((line.strip(), *match.groups()))
    print(f"{len(clips)} generated clip(s) in the bucket")

    out = Path(args.out)
    rows = []
    for url, run_id, change_id, method in sorted(clips, key=lambda c: c[1]):
        if args.method and method != args.method:
            continue
        run = runs.get(run_id)
        if run is None:
            print(f"  skip {change_id}: {run_id} is not in this store")
            continue
        change = next((c for c in db.changes(run_id) if c.id == change_id), None)
        finding = findings.get(change.finding_id) if change is not None else None
        asset = Path(run.asset_path).stem or run.asset_path
        start = finding.t_start if finding else 0.0
        end = finding.t_end if finding else 0.0
        market = finding.market if finding else ""
        rule = finding.rule_id if finding else ""
        stem = "__".join(filter(None, (
            slug(asset), slug(market), slug(rule),
            f"{method}", f"{start:06.1f}-{end:06.1f}", change_id)))
        rows.append({
            "file": stem, "asset": asset, "run": run_id, "change": change_id,
            "method": method, "market": market, "rule": rule,
            "t_start": f"{start:.1f}", "t_end": f"{end:.1f}",
            "instruction": (change.description if change else ""),
            "rationale": (finding.rationale if finding else ""),
        })
        if args.dry_run:
            print(f"  {stem}")
            continue
        out.mkdir(parents=True, exist_ok=True)
        after = out / f"{stem}__after.mp4"
        if not after.is_file():
            sh("gcloud", "storage", "cp", url, str(after))
        before = out / f"{stem}__before.mp4"
        span = max(0.4, end - start)
        if not before.is_file() and end > 0:
            try:
                sh("ffmpeg", "-y", "-v", "error",
                   "-ss", f"{max(0.0, start):.2f}",
                   "-i", f"{BASE}/runs/{run_id}/media/original",
                   "-t", f"{span:.2f}", "-an",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   str(before))
            except subprocess.CalledProcessError as exc:
                print(f"  {stem}: the master would not cut ({exc.stderr.strip()[:80]})")
        print(f"  {stem}: {'pair' if before.is_file() else 'after only'}")

    if args.dry_run or not rows:
        print(f"\n{len(rows)} pair(s) would be written to {out}")
        return 0

    with (out / "index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (out / "README.txt").write_text(
        "The Media Customs: every generated span, before and after.\n\n"
        "One pair per edit. __before.mp4 is the span as delivered, cut from\n"
        "the original master on the finding's own timecodes. __after.mp4 is\n"
        "the model's own output, copied unmodified: *_omni is Gemini Omni\n"
        "rewriting the span in place, *_bridge is Veo generating the motion\n"
        "between two edited frames.\n\n"
        "The filename is film__market__rule__method__seconds__change-id, and\n"
        "index.csv carries the instruction the remediator was given and the\n"
        "rationale the market wrote against the shot.\n")
    print(f"\n{len(rows)} pair(s) in {out}")
    print(f"  index.csv and README.txt alongside them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
