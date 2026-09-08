"""Beat 1: the commercial itself, cut to five moving shots. No app UI.

    python3 scripts/make_opening.py  ->  docs/video/beats/01/ad.mp4
"""
import os, subprocess
SRC = "docs/samples/test_ad.mp4"
OUT = "docs/video/beats/01"
# the shots that carry the landmines the narration names
CLIPS = [(0.5, 5.5), (15.0, 4.5), (20.0, 4.5), (36.0, 4.5), (44.0, 5.5)]

os.makedirs(OUT, exist_ok=True)
parts = []
for i, (start, length) in enumerate(CLIPS):
    dst = f"{OUT}/c{i}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", SRC,
                    "-t", str(length), "-an",
                    "-vf", "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
                           "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", dst], check=True)
    parts.append(os.path.basename(dst))
with open(f"{OUT}/list.txt", "w") as f:
    for p in parts:
        f.write(f"file '{p}'\n")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", f"{OUT}/list.txt", "-c", "copy", f"{OUT}/raw.mp4"], check=True)

# the title, held over the opening shots and lifted off them
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{OUT}/raw.mp4",
                "-i", "docs/video/title.png",
                "-filter_complex",
                "[0:v]fade=t=in:st=0:d=0.7[b];"
                "[1:v]format=rgba,fade=t=out:st=4.4:d=1.1:alpha=1[t];"
                "[b][t]overlay=0:0:enable='lt(t,5.6)'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", f"{OUT}/ad.mp4"], check=True)
os.remove(f"{OUT}/raw.mp4")
for p in parts:
    os.remove(f"{OUT}/{p}")
print("beat 1 built:", f"{OUT}/ad.mp4")
