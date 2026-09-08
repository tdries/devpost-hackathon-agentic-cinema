"""Beat 1: the commercial, cut so each shot lands on the line about it.

    python3 scripts/make_opening.py  ->  docs/video/beats/01/ad.mp4

The narration for beat 1 is spoken in four parts and render_voiceover writes
their offsets to marks.json, so the wine shot is on screen while the wine is
being talked about rather than by luck.
"""
import json, os, subprocess

SRC = "docs/samples/test_ad.mp4"
OUT = "docs/video/beats/01"
# one source shot per spoken part, in order
SHOTS = [20.0,    # a beach, for "launch this globally"
         0.6,     # the terrace: two women, glasses of red wine
         15.0,    # a thumbs up to camera
         49.6,    # the bottle, its label to camera
         44.0]    # two at a table, the shot that gets fixed later

offs = json.load(open("docs/voiceover/marks.json"))["1"]
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", "docs/voiceover/01.wav"],
                           capture_output=True, text=True).stdout)
lens = [b - a for a, b in zip(offs, offs[1:])] + [dur - offs[-1] + 0.8]

os.makedirs(OUT, exist_ok=True)
parts = []
for i, (start, length) in enumerate(zip(SHOTS, lens)):
    dst = f"{OUT}/c{i}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", SRC,
                    "-t", f"{length:.3f}", "-an",
                    "-vf", "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
                           "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", dst], check=True)
    parts.append(os.path.basename(dst))
    print(f"part {i + 1}: {start:5.1f}s of the ad, held {length:4.1f}s")

with open(f"{OUT}/list.txt", "w") as f:
    for p in parts:
        f.write(f"file '{p}'\n")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", f"{OUT}/list.txt", "-c", "copy", f"{OUT}/raw.mp4"], check=True)

# the title, held over the opening shots and lifted off them
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{OUT}/raw.mp4",
                "-loop", "1", "-t", f"{dur + 1:.2f}", "-i", "docs/video/title.png",
                "-filter_complex",
                "[0:v]fade=t=in:st=0:d=0.7[b];"
                "[1:v]format=rgba,fade=t=out:st=3.6:d=1.0:alpha=1[t];"
                "[b][t]overlay=0:0:enable='lt(t,4.7)'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", f"{OUT}/ad.mp4"], check=True)
for p in parts:
    os.remove(f"{OUT}/{p}")
os.remove(f"{OUT}/raw.mp4")
print("beat 1 built")
