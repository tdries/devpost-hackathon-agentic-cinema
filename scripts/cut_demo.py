"""Cut each recorded beat to its voiceover length and lay the narration over it.

    python3 scripts/cut_demo.py   ->  docs/video/demo.mp4

Each beat is trimmed from its start to the length of docs/voiceover/NN.wav plus a
half-second of held frame, so the cut lands on the pause the audio already has.
"""
import glob, os, subprocess

OUT = "docs/video"
GAP = 0.4
# seconds to drop off the head of a beat that had to wait for something to paint
SKIP = {13: 30.0}   # the insight panels need that long to paint


def dur(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    if out in ("", "N/A"):   # webm from the recorder can lack a duration header
        out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                              "-show_entries", "stream=nb_read_packets,avg_frame_rate",
                              "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
        packets, rate = out.split(",")[0], out.split(",")[1]
        num, den = (rate.split("/") + ["1"])[:2]
        return int(packets) / (float(num) / float(den or 1))
    return float(out)


os.makedirs(f"{OUT}/cut", exist_ok=True)
parts = []
for n in range(1, 15):
    src = sorted(glob.glob(f"{OUT}/beats/{n:02d}/*.webm") + glob.glob(f"{OUT}/beats/{n:02d}/*.mp4"))[0]
    want = dur(f"docs/voiceover/{n:02d}.wav") + GAP
    dst = f"{OUT}/cut/{n:02d}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{SKIP.get(n, 0):.1f}", "-i", src, "-t", f"{want:.3f}",
                    "-vf", "fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
                           "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", dst], check=True)
    have = dur(dst)
    print(f"{n:02d}  want {want:5.1f}s  got {have:5.1f}s  {'SHORT' if have < want - 0.3 else ''}")
    parts.append(dst)

with open(f"{OUT}/cut/list.txt", "w") as f:
    for p in parts:
        f.write(f"file '{os.path.basename(p)}'\n")

subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", f"{OUT}/cut/list.txt", "-c", "copy", f"{OUT}/silent.mp4"], check=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{OUT}/silent.mp4",
                "-i", "docs/voiceover/demo-voiceover.wav", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                f"{OUT}/demo.mp4"], check=True)
print("video", dur(f"{OUT}/silent.mp4"), "audio", dur("docs/voiceover/demo-voiceover.wav"))
