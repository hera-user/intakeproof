"""Rebuild the disclosed screen-capture walkthrough with locally installed FFmpeg."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(work):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("Install FFmpeg from its official distribution before rendering the optional video.")
    candidates = [Path("C:/Windows/Fonts/segoeui.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/System/Library/Fonts/Helvetica.ttc")]
    font = next((path for path in candidates if path.is_file()), None)
    if not font:
        raise SystemExit("A supported system font was not found.")
    font_filter = font.as_posix().replace(":", "\\:")
    metadata = json.loads((ROOT / "demo/scenes.json").read_text("utf-8"))
    work.mkdir(parents=True, exist_ok=True)
    (work / "disclosure.txt").write_text("ACTUAL APP CAPTURES  /  SYNTHETIC DATA  /  LOCAL EXECUTION  /  PACED WALKTHROUGH", encoding="utf-8")
    clips = []
    for index, scene in enumerate(metadata["scenes"], 1):
        caption = f"caption-{index:02d}.txt"
        (work / caption).write_text(scene["caption"], encoding="utf-8")
        clip = work / f"clip-{index:02d}.mp4"
        image = ROOT / "demo/frames" / scene["image"]
        filters = (
            "scale=1280:714:force_original_aspect_ratio=decrease,"
            "pad=1280:800:(ow-iw)/2:0:color=0xf5f4ef,"
            "drawbox=x=0:y=714:w=1280:h=86:color=0x1b322a:t=fill,"
            f"drawtext=fontfile='{font_filter}':textfile='{caption}':x=28:y=733:fontsize=21:fontcolor=white,"
            f"drawtext=fontfile='{font_filter}':textfile='disclosure.txt':x=28:y=778:fontsize=10:fontcolor=0xd8eaa9"
        )
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-loop", "1", "-framerate", "15", "-i", str(image), "-t", str(scene["seconds"]), "-vf", filters, "-c:v", "libx264", "-preset", "fast", "-tune", "stillimage", "-crf", "18", "-pix_fmt", "yuv420p", str(clip)], cwd=work, check=True)
        clips.append(clip)
    playlist = work / "clips.txt"
    playlist.write_text("".join("file '" + clip.as_posix().replace("'", "'\\''") + "'\n" for clip in clips), encoding="utf-8")
    destination = ROOT / "docs/demo.mp4"
    destination.parent.mkdir(exist_ok=True)
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(playlist), "-c", "copy", "-movflags", "+faststart", str(destination)], check=True)
    probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration,size:stream=codec_name,width,height,pix_fmt", "-of", "json", str(destination)], capture_output=True, text=True, check=True)
    actual = json.loads(probe.stdout)
    assert abs(float(actual["format"]["duration"]) - sum(s["seconds"] for s in metadata["scenes"])) < 0.2
    print(json.dumps({"file": str(destination), "method": metadata["method"], "probe": actual}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    if args.work_dir:
        render(args.work_dir.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="intakeproof-video-") as directory:
            render(Path(directory))
