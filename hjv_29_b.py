#!/usr/bin/env python3
"""
HV_OP - V2.9.9-G7: tmpfs Pipeline + Dual Audio Verification + Tree-Rebuilding Vault
              + GATE-7 Duration Mismatch Triage with Full Diagnostics
------------------------------------------------------------------------------------
Gate Sequence:

  NORMAL:  GATE-1 → GATE-2 → GATE-3 → GATE-4 → GATE-5 → GATE-6

  MISMATCH (GATE-1 or GATE-2 fail):
           GATE-7 invoked → GATE-3 → GATE-4 → GATE-5 → GATE-6
              │
              ├── ALL pass → save to mismatched_vault, source KEPT
              │     ├── Δ < 2s  → save HEVC+Copy muxed
              │     └── Δ ≥ 2s  → save video + audio SEPARATELY
              │
              └── ANY fail → processed DELETED, source kept
"""

import os
import sys
import glob
import subprocess
import shutil
import time
import getpass
import uuid
import signal
import atexit
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
import ujson as json

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
except ImportError:
    print("FATAL: Dependencies missing. Run: pip install rich ujson")
    sys.exit(1)

console = Console()

# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------
_BLACK_HOLE_DIR = Path('~/system_clean/to_be_removed')
_TMFS_DIR = Path('/dev/shm/hv_op_workspace')
_USER = getpass.getuser()
_FFMPEG_DIR = Path(f'/home/{_USER}/ffmpeg/bin')
SOURCE_REMOVAL = True

# GATE-6 Dynamic Savings Rules
_MIN_ABSOLUTE_SAVINGS_MB = 40
_MIN_PERCENTAGE_SAVINGS = 0.10
_MICRO_FILE_THRESHOLD_MB = 5.0
_MICRO_FILE_PERCENTAGE_SAVINGS = 0.20

# GATE-7: Duration mismatch threshold (seconds)
_GATE7_MISMATCH_THRESHOLD = 2.0

# V3.2 Opus 6-tier psychoacoustic ladder
OPUS_LADDER = [
    {"limit": 48,          "br": "12k",  "frame": "60", "app": "voip"},
    {"limit": 96,          "br": "48k",  "frame": "40", "app": "audio"},
    {"limit": 160,         "br": "96k",  "frame": "20", "app": "audio"},
    {"limit": 256,         "br": "160k", "frame": "20", "app": "audio"},
    {"limit": 448,         "br": "256k", "frame": "20", "app": "audio"},
    {"limit": sys.maxsize, "br": "320k", "frame": "20", "app": "audio"},
]


def _cleanup_tmpfs_exit():
    if _TMFS_DIR.exists():
        shutil.rmtree(_TMFS_DIR, ignore_errors=True)

def _cleanup_tmpfs_signal(signum: int, frame: Any) -> None:
    console.print("\n[yellow]Interrupted. Cleaning tmpfs workspace...[/]")
    _cleanup_tmpfs_exit()
    sys.exit(1)

signal.signal(signal.SIGINT, _cleanup_tmpfs_signal)
signal.signal(signal.SIGTERM, _cleanup_tmpfs_signal)
atexit.register(_cleanup_tmpfs_exit)


def _resolve_binary(name: str) -> str:
    custom = _FFMPEG_DIR / name
    if _FFMPEG_DIR.exists() and custom.exists():
        return str(custom)
    found = shutil.which(name)
    if not found:
        console.print(f"[bold red]FATAL: '{name}' not found in custom path or system PATH.[/]")
        sys.exit(1)
    return found

_FFMPEG = _resolve_binary('ffmpeg')
_FFPROBE = _resolve_binary('ffprobe')


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1048576:
        return f"{size_bytes // 1024}KB"
    return f"{size_bytes // 1024 // 1024}MB"


def _black_hole(path: Path) -> None:
    if not path.exists():
        return
    try:
        _BLACK_HOLE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        console.print(f"[red]Cannot create black hole dir: {e}. File left in place.[/]")
        return
    dest = _BLACK_HOLE_DIR / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
    try:
        shutil.move(str(path), str(dest))
    except OSError:
        shutil.copyfile(str(path), str(dest))
        path.unlink()


def _detect_gpu_backend() -> Optional[str]:
    candidates = sorted(glob.glob('/dev/dri/renderD*'))
    if not candidates:
        return None
    for dev in candidates:
        try:
            test_cmd = [
                _FFMPEG, "-v", "error",
                "-init_hw_device", f"vaapi=hw:{dev}",
                "-filter_hw_device", "hw",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-vf", "format=nv12,hwupload",
                "-c:v", "hevc_vaapi",
                "-f", "null", "-"
            ]
            subprocess.run(test_cmd, check=True, capture_output=True, timeout=15)
            return dev
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class AutomatedHvOpEngine:
    def __init__(self, target_directory: str, output_directory: str = ""):
        self.input_dir = Path(target_directory).resolve()
        self.output_dir = Path(output_directory).resolve() if output_directory.strip() else self.input_dir

        self.vault_dir = self.output_dir / "universal_vault"
        self.archive_dir = self.output_dir / "prcs_cmpl"
        self.bypass_dir = self.output_dir / "bypass_file"
        self.black_prob_dir = self.output_dir / "hevc_black_probable"
        self.mismatch_dir = self.output_dir / "mismatched_vault"

        for d in (self.vault_dir, self.archive_dir, self.bypass_dir,
                  self.black_prob_dir, self.mismatch_dir):
            d.mkdir(parents=True, exist_ok=True)

        if _TMFS_DIR.exists():
            shutil.rmtree(_TMFS_DIR)
        self.prcs_dir = _TMFS_DIR / "prcs_file"
        self.prcs_dir.mkdir(parents=True, exist_ok=True)

        self.media_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.webm',
            '.flv', '.wmv', '.m4v', '.ts', '.mts', '.3gp'
        }
        self.session_summary: List[Tuple[str, str, str, str]] = []
        self._playable_cache: Dict[str, bool] = {}

        self.gpu_device = _detect_gpu_backend()
        if self.gpu_device:
            console.print(f"[bold green]GPU backend detected:[/] VAAPI via {self.gpu_device}")
        else:
            console.print("[yellow]No usable VAAPI GPU backend found — running CPU-only (libx265) for this session.[/]")

    # -----------------------------------------------------------------------
    # Tree Rebuilding Helpers
    # -----------------------------------------------------------------------
    def _get_vault_path(self, src: Path) -> Path:
        try:
            rel_path = src.relative_to(self.input_dir)
        except ValueError:
            rel_path = Path(src.name)
        dest_dir = self.vault_dir / rel_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean = dest_dir / f"{rel_path.stem}_vault{rel_path.suffix}"
        if not clean.exists():
            return clean
        return dest_dir / f"{rel_path.stem}_{uuid.uuid4().hex[:8]}_vault{rel_path.suffix}"

    def _get_mismatch_path(self, src: Path, suffix: str) -> Path:
        try:
            rel_path = src.relative_to(self.input_dir)
        except ValueError:
            rel_path = Path(src.name)
        dest_dir = self.mismatch_dir / rel_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean = dest_dir / f"{rel_path.stem}_{suffix}{rel_path.suffix}"
        if not clean.exists():
            return clean
        return dest_dir / f"{rel_path.stem}_{uuid.uuid4().hex[:8]}_{suffix}{rel_path.suffix}"

    # -----------------------------------------------------------------------
    # Probe
    # -----------------------------------------------------------------------
    def _probe(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            cmd = [_FFPROBE, "-v", "quiet", "-print_format", "json",
                   "-show_streams", "-show_format", str(path)]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout)
        except Exception:
            return None

    def _is_playable(self, path: Path) -> bool:
        path_str = str(path)
        if path_str in self._playable_cache:
            return self._playable_cache[path_str]
        try:
            subprocess.run([_FFMPEG, "-v", "error", "-i", str(path),
                           "-f", "null", "-"], check=True, capture_output=True)
            self._playable_cache[path_str] = True
            return True
        except subprocess.CalledProcessError:
            self._playable_cache[path_str] = False
            return False

    # -----------------------------------------------------------------------
    # GATE-7 Diagnostic play
    # -----------------------------------------------------------------------
    def _diagnostic_play(self, path: Path) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                [_FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
                capture_output=True, text=True, timeout=120
            )
            errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
            if result.returncode == 0 and not errors:
                return True, []
            if result.returncode == 0 and errors:
                return True, errors
            return False, errors
        except subprocess.TimeoutExpired:
            return False, ["Decode timed out (120s)"]
        except Exception as e:
            return False, [f"Exception during decode: {e}"]

    # -----------------------------------------------------------------------
    # GATE-7 Diagnostic probe
    # -----------------------------------------------------------------------
    def _diagnostic_probe(self, path: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        errors = []
        try:
            cmd = [_FFPROBE, "-v", "error", "-print_format", "json",
                   "-show_streams", "-show_format", str(path)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            errors = [line.strip() for line in result.stderr.splitlines() if line.strip()]
            if result.returncode != 0:
                return None, errors + [f"ffprobe exited with code {result.returncode}"]
            data = json.loads(result.stdout)
            if 'streams' not in data:
                return None, errors + ["No streams found in probe output"]
            return data, errors
        except json.JSONDecodeError:
            return None, errors + ["Probe output not valid JSON — container corrupt"]
        except subprocess.TimeoutExpired:
            return None, errors + ["Probe timed out"]
        except Exception as e:
            return None, errors + [f"Exception during probe: {e}"]

    # -----------------------------------------------------------------------
    # Interlace Detection
    # -----------------------------------------------------------------------
    def _is_interlaced(self, path: Path) -> bool:
        try:
            cmd = [_FFPROBE, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=field_order", "-of", "csv=p=0", str(path)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            field_order = result.stdout.strip().lower()
            return field_order in ('tt', 'tb', 'bt', 'bb')
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Resolution-adaptive Quality Settings
    # -----------------------------------------------------------------------
    def _compute_video_quality(self, height: int, use_gpu: bool) -> str:
        if use_gpu:
            if height >= 2160: return "19"
            if height >= 1080: return "22"
            if height >= 720:  return "25"
            return "27"
        else:
            if height >= 2160: return "20"
            if height >= 1080: return "23"
            if height >= 720:  return "26"
            return "28"

    # -----------------------------------------------------------------------
    # Audio ladder
    # -----------------------------------------------------------------------
    def _pick_opus_tier(self, br_kbps: float) -> Dict[str, str]:
        for tier in OPUS_LADDER:
            if br_kbps <= tier["limit"]:
                return tier
        return OPUS_LADDER[-1]

    # -----------------------------------------------------------------------
    # Potato tier
    # -----------------------------------------------------------------------
    def _potato_tier(self, src_size: int, tgt_size: int) -> str:
        if src_size <= 0:
            return "Unknown"
        savings = ((src_size - tgt_size) / src_size) * 100
        if savings >= 95:  return f"Transparent (-{savings:.1f}%)"
        if savings >= 75:  return f"Optimal (-{savings:.1f}%)"
        if savings >= 40:  return f"Dense (-{savings:.1f}%)"
        if savings >= 10:  return f"Bland Potato (-{savings:.1f}%)"
        if savings > 0:    return f"Marginal (-{savings:.1f}%)"
        return f"Negative Delta (+{abs(savings):.1f}%)"

    # -----------------------------------------------------------------------
    # GATE-6 size check logic (reusable for both normal and GATE-7 paths)
    # -----------------------------------------------------------------------
    def _gate6_check(self, src_size: int, out_size: int, label: str = "") -> Tuple[bool, str]:
        """Run GATE-6 size comparison. Returns (pass, detail)."""
        reduction = (src_size - out_size) / src_size if src_size > 0 else 0
        abs_savings_mb = (src_size - out_size) / (1024 * 1024)
        src_size_mb = src_size / (1024 * 1024)

        percent_pass = False
        absolute_pass = False

        if src_size_mb < _MICRO_FILE_THRESHOLD_MB:
            percent_pass = reduction >= _MICRO_FILE_PERCENTAGE_SAVINGS
            if not percent_pass:
                return False, f"GATE-6 FAIL: {label} saves {reduction*100:.1f}% (need ≥{_MICRO_FILE_PERCENTAGE_SAVINGS*100:.0f}% for micro-file)"
        else:
            percent_pass = reduction >= _MIN_PERCENTAGE_SAVINGS
            absolute_pass = abs_savings_mb >= _MIN_ABSOLUTE_SAVINGS_MB
            if not percent_pass and not absolute_pass:
                return False, f"GATE-6 FAIL: {label} saves {reduction*100:.1f}% / {abs_savings_mb:.1f}MB (need ≥{_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR ≥{_MIN_ABSOLUTE_SAVINGS_MB}MB)"

        detail = f"GATE-6 PASS: {label} saves {reduction*100:.1f}% ({abs_savings_mb:.1f}MB)"
        return True, detail

    # -----------------------------------------------------------------------
    # HEVC video-only encode (tmpfs)
    # -----------------------------------------------------------------------
    def _encode_hevc_video(self, src: Path, video_streams: List[Dict[str, Any]],
                           use_fallback: bool) -> Optional[Path]:
        tmp_video = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_hevc_tmp.mkv"
        if tmp_video.exists():
            tmp_video.unlink()

        cmd_args = []
        height = int(video_streams[0].get('height', 720))
        quality = self._compute_video_quality(height, not use_fallback)

        is_interlaced = self._is_interlaced(src)
        if is_interlaced:
            console.print("  [dim]Interlaced content detected — enabling yadif deinterlace[/]")

        v_idx = 0
        if use_fallback:
            filter_chain = "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
            if is_interlaced:
                filter_chain = f"yadif,{filter_chain}"
            cmd_args.extend([
                "-map", f"0:v:{v_idx}",
                f"-c:v:{v_idx}", "libx265",
                "-crf", quality,
                "-preset", "slow",
                f"-vf:v:{v_idx}", filter_chain
            ])
        else:
            filter_chain = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload"
            cmd_args.extend([
                "-map", f"0:v:{v_idx}",
                f"-c:v:{v_idx}", "hevc_vaapi",
                "-global_quality", quality,
                f"-vf:v:{v_idx}", filter_chain
            ])

        base = [_FFMPEG, "-y"]
        if not use_fallback:
            base.extend(["-init_hw_device", f"vaapi=hw:{self.gpu_device}", "-filter_hw_device", "hw"])

        cmd = base + ["-v", "error", "-stats", "-i", str(src)] + cmd_args + ["-an", str(tmp_video)]

        try:
            process = subprocess.Popen(cmd, stderr=subprocess.PIPE, universal_newlines=True, bufsize=1)
            buffer = ""
            while True:
                char = process.stderr.read(1)
                if char == "" and process.poll() is not None:
                    break
                if char:
                    buffer += char
                    if char == '\r' or char == '\n':
                        line = buffer.strip()
                        buffer = ""
                        if "time=" in line:
                            time_part = [p for p in line.split() if p.startswith("time=")]
                            speed_part = [p for p in line.split() if p.startswith("speed=")]
                            speed = speed_part[0] if speed_part else ""
                            if time_part:
                                console.print(f"\r  [dim]Encoding: {time_part[0]} {speed}[/]", end="")
            console.print()
            process.wait()
            if process.returncode != 0:
                if tmp_video.exists():
                    tmp_video.unlink()
                return None
            return tmp_video
        except OSError as e:
            console.print(f"\n  [bold red]RAM DISK FULL during video encode: {e}[/]")
            if tmp_video.exists():
                tmp_video.unlink()
            return None

    # -----------------------------------------------------------------------
    # Mux single candidate (tmpfs) with V3.2 Opus args
    # -----------------------------------------------------------------------
    def _mux_candidate(self, src: Path, tmp_video: Path, audio_streams: List[Dict[str, Any]],
                       audio_combo: Tuple[int, ...], label: str,
                       subtitle_count: int = 0, attachment_count: int = 0) -> Optional[Path]:
        out = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_{label}.mkv"
        if out.exists():
            out.unlink()

        cmd = [_FFMPEG, "-y", "-v", "error", "-i", str(tmp_video), "-i", str(src)]

        for a_idx, track in enumerate(audio_streams):
            is_opus = audio_combo[a_idx] if a_idx < len(audio_combo) else False
            if is_opus:
                src_br = track.get("bit_rate")
                try:
                    src_kbps = (int(src_br) / 1000) if src_br else 96.0
                except (ValueError, TypeError):
                    src_kbps = 96.0
                tier = self._pick_opus_tier(src_kbps)
                cmd.extend([
                    "-map", f"1:a:{a_idx}",
                    f"-c:a:{a_idx}", "libopus",
                    f"-b:a:{a_idx}", tier["br"],
                    f"-vbr:{a_idx}", "on",
                    f"-compression_level:{a_idx}", "10",
                    f"-frame_duration:{a_idx}", tier["frame"],
                    f"-application:{a_idx}", tier["app"]
                ])
            else:
                cmd.extend([
                    "-map", f"1:a:{a_idx}",
                    f"-c:a:{a_idx}", "copy"
                ])

        for s_idx in range(subtitle_count):
            cmd.extend(["-map", f"1:s:{s_idx}", f"-c:s:{s_idx}", "copy"])

        for t_idx in range(attachment_count):
            cmd.extend(["-map", f"1:t:{t_idx}", f"-c:t:{t_idx}", "copy"])

        cmd.extend(["-map", "0:v:0", "-c:v:0", "copy", "-map_metadata", "1", str(out)])

        try:
            subprocess.run(cmd, check=True)
            return out
        except subprocess.CalledProcessError:
            if out.exists():
                out.unlink()
            return None
        except OSError as e:
            console.print(f"  [bold red]RAM DISK FULL during audio mux: {e}[/]")
            if out.exists():
                out.unlink()
            return None

    # -----------------------------------------------------------------------
    # Generate exactly two candidates: All-Opus and All-Copy
    # -----------------------------------------------------------------------
    def _mux_audio_candidates(self, src: Path, tmp_video: Path,
                                     audio_streams: List[Dict[str, Any]],
                                     subtitle_count: int = 0,
                                     attachment_count: int = 0) -> Tuple[Optional[Path], Optional[Path]]:
        if not audio_streams:
            out = self._mux_candidate(src, tmp_video, [], (),
                                       label="cand_mute",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
            return out, None

        n = len(audio_streams)
        opus_combo = tuple([1] * n)
        copy_combo = tuple([0] * n)

        out_opus = self._mux_candidate(src, tmp_video, audio_streams, opus_combo,
                                       label="cand_opus",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
        out_copy = self._mux_candidate(src, tmp_video, audio_streams, copy_combo,
                                       label="cand_copy",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)

        return out_opus, out_copy

    # -----------------------------------------------------------------------
    # Black frame ratio detection
    # -----------------------------------------------------------------------
    def _black_ratio(self, path: Path) -> float:
        try:
            meta = self._probe(path)
            if not meta:
                return 0.0
            duration = float(meta.get('format', {}).get('duration', 0) or 0)
            if duration > 60:
                sample_duration = min(45, duration / 2 - 1)
                if sample_duration <= 0:
                    sample_duration = 30
                cmd = [
                    _FFMPEG, "-v", "error",
                    "-i", str(path), "-ss", "0", "-t", str(sample_duration),
                    "-i", str(path), "-ss", str(duration - sample_duration), "-t", str(sample_duration),
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]",
                    "-vf", "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10",
                    "-an", "-f", "null", "-"
                ]
                total_check_duration = sample_duration * 2
            else:
                cmd = [
                    _FFMPEG, "-v", "error", "-i", str(path),
                    "-vf", "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10",
                    "-an", "-f", "null", "-"
                ]
                total_check_duration = duration
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            lines = result.stderr.splitlines()
            black_duration = 0.0
            for line in lines:
                if "black_duration" in line:
                    for part in line.split():
                        if part.startswith("black_duration:"):
                            try:
                                black_duration += float(part.split(":")[1])
                            except ValueError:
                                pass
            if total_check_duration > 0:
                return black_duration / total_check_duration
            return 0.0
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # GATE-1/2: Duration check only
    # Returns mismatch_info dict if GATE-1 or GATE-2 fail, else None
    # -----------------------------------------------------------------------
    def _check_gate12(self, src: Path, src_meta: Dict[str, Any],
                      output: Path) -> Optional[Dict[str, float]]:

        out_meta = self._probe(output)
        if not out_meta:
            # Can't even probe — this is a harder problem than GATE-1/2
            return None

        src_fmt_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
        out_fmt_dur = float(out_meta.get('format', {}).get('duration', 0) or 0)

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']
        src_a = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
        out_a = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'audio']

        mismatch_info: Optional[Dict[str, float]] = None

        # GATE-1: Video duration
        if src_v and out_v:
            try:
                sv_dur = float(src_v[0].get('duration') or src_fmt_dur)
                ov_dur = float(out_v[0].get('duration') or out_fmt_dur)
                if sv_dur > 0 and abs(sv_dur - ov_dur) > 0.5:
                    delta = abs(sv_dur - ov_dur)
                    mismatch_info = {"video_delta": delta, "audio_delta": 0.0, "max_delta": 0.0}
                    console.print(f"  [bold yellow]GATE-1 FAIL:[/] Video duration mismatch ({delta:.2f}s)")
            except (ValueError, TypeError):
                pass

        # GATE-2: Audio duration
        if src_a and out_a:
            try:
                sa_dur = float(src_a[0].get('duration') or src_fmt_dur)
                oa_dur = float(out_a[0].get('duration') or out_fmt_dur)
                if sa_dur > 0 and abs(sa_dur - oa_dur) > 0.5:
                    delta = abs(sa_dur - oa_dur)
                    if mismatch_info is None:
                        mismatch_info = {"video_delta": 0.0, "audio_delta": delta, "max_delta": 0.0}
                    else:
                        mismatch_info["audio_delta"] = delta
                    console.print(f"  [bold yellow]GATE-2 FAIL:[/] Audio duration mismatch ({delta:.2f}s)")
            except (ValueError, TypeError):
                pass

        if mismatch_info is not None:
            mismatch_info["max_delta"] = max(mismatch_info["video_delta"], mismatch_info["audio_delta"])

        return mismatch_info

    # -----------------------------------------------------------------------
    # GATE-3/4/5: Remaining quality checks (hard gates)
    # Returns (passed, gate_failed, detail, black_probable)
    # -----------------------------------------------------------------------
    def _check_gate345(self, src: Path, src_meta: Dict[str, Any],
                       output: Path) -> Tuple[bool, str, str, bool]:

        out_meta = self._probe(output)
        if not out_meta:
            return False, "GATE-0", "Cannot probe output metadata", False

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']

        # GATE-3: Resolution
        if src_v:
            if not out_v:
                return False, "GATE-3", "Video stream lost in output", False
            try:
                sw, sh = int(src_v[0].get('width', 0)), int(src_v[0].get('height', 0))
                ow, oh = int(out_v[0].get('width', 0)), int(out_v[0].get('height', 0))
                exp_w, exp_h = (sw // 2) * 2, (sh // 2) * 2
                if not ((ow == exp_w and oh == exp_h) or (ow == exp_h and oh == exp_w)):
                    return False, "GATE-3", f"Dimension mismatch (src {exp_w}x{exp_h}, out {ow}x{oh})", False
            except (ValueError, TypeError) as e:
                return False, "GATE-3", f"Dimension unreadable: {e}", False

        # GATE-4: Integrity
        out_sz = output.stat().st_size
        if out_sz == 0:
            return False, "GATE-4", "Output is zero bytes", False
        if str(output) not in self._playable_cache:
            if not self._is_playable(output):
                return False, "GATE-4", "Output failed null-render decode", False

        # GATE-5: Black ratio
        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | out: {out_black*100:.1f}%[/]")
            if out_black >= 0.05 and out_black > src_black + 0.02:
                return True, "", f"Output {out_black*100:.1f}% black vs source {src_black*100:.1f}%", True

        return True, "", "GATE-3/4/5 passed", False

    # -----------------------------------------------------------------------
    # Normal early gates: GATE-1 → GATE-2 → GATE-3 → GATE-4 → GATE-5
    # Returns (passed, gate_failed, detail, black_probable, mismatch_info)
    # mismatch_info != None means GATE-1/2 failed → GATE-7 should be invoked
    # -----------------------------------------------------------------------
    def _verify_early_gates(self, src: Path, src_meta: Dict[str, Any],
                            output: Path) -> Tuple[bool, str, str, bool, Optional[Dict[str, float]]]:

        # Step 1: GATE-1 → GATE-2
        mismatch_info = self._check_gate12(src, src_meta, output)

        # If GATE-1/2 failed, we STILL run GATE-3/4/5 to know the full status
        # The caller decides whether to go normal or GATE-7 path

        # Step 2: GATE-3 → GATE-4 → GATE-5
        passed, gate_failed, detail, black_probable = self._check_gate345(src, src_meta, output)

        return passed, gate_failed, detail, black_probable, mismatch_info

    # -----------------------------------------------------------------------
    # GATE-7: Full diagnostics display
    # -----------------------------------------------------------------------
    def _gate7_diagnostics(self, src: Path, src_meta: Dict[str, Any],
                           tmp_video: Path, mismatch_info: Dict[str, float]) -> None:

        max_delta = mismatch_info["max_delta"]
        vid_delta = mismatch_info["video_delta"]
        aud_delta = mismatch_info["audio_delta"]

        console.print(Panel(
            f"[bold yellow]⚠ GATE-7 INVOKED[/]\n"
            f"[dim]Video Δ: {vid_delta:.2f}s | Audio Δ: {aud_delta:.2f}s | Max Δ: {max_delta:.2f}s[/]",
            border_style="yellow"
        ))

        # --- Source diagnostics ---
        console.print("  [bold cyan]═══ Source File Diagnostics ═══[/]")
        src_playable, src_errors = self._diagnostic_play(src)
        src_probe_ok, src_probe_errors = self._diagnostic_probe(src)
        console.print(f"    Playable: {'[green]✓[/]' if src_playable else '[bold red]✗[/]'}  |  "
                      f"Probe: {'[green]✓[/]' if src_probe_ok else '[bold red]✗[/]'}")
        if src_errors:
            console.print(f"    [yellow]Decode warnings/errors ({len(src_errors)}):[/]")
            for err in src_errors[:8]:
                console.print(f"      [dim yellow]{err}[/]")
            if len(src_errors) > 8:
                console.print(f"      [dim yellow]... and {len(src_errors)-8} more[/]")
        if src_probe_errors:
            console.print(f"    [yellow]Probe warnings/errors ({len(src_probe_errors)}):[/]")
            for err in src_probe_errors[:8]:
                console.print(f"      [dim yellow]{err}[/]")
            if len(src_probe_errors) > 8:
                console.print(f"      [dim yellow]... and {len(src_probe_errors)-8} more[/]")

        # Source stream health
        if src_meta:
            for s_idx, stream in enumerate(src_meta.get('streams', [])):
                codec_type = stream.get('codec_type', '?')
                codec_name = stream.get('codec_name', '?')
                dur = stream.get('duration', '?')
                nb_frames = stream.get('nb_frames', '?')
                issues = []
                if stream.get('codec_tag') == '0x0000':
                    issues.append("Zero codec tag")
                if dur == 'N/A' or dur == 0:
                    issues.append("Duration unreadable")
                if codec_type == 'video' and nb_frames == 'N/A':
                    issues.append("Frame count unreadable")
                issue_str = f" [yellow]{'; '.join(issues)}[/]" if issues else ""
                console.print(f"    Stream {s_idx} ({codec_type}/{codec_name}): dur={dur}s frames={nb_frames}{issue_str}")

        # Moov atom check
        if src.suffix.lower() in ('.mp4', '.m4v', '.3gp'):
            try:
                moov_cmd = [_FFPROBE, "-v", "error", "-show_entries",
                           "format_tags=compatible_brands:major_brand:minor_version",
                           "-of", "csv=p=0", str(src)]
                moov_result = subprocess.run(moov_cmd, capture_output=True, text=True, timeout=10)
                if moov_result.stderr.strip():
                    console.print(f"    [yellow]Container structural issues:[/]")
                    for line in moov_result.stderr.strip().splitlines()[:4]:
                        console.print(f"      [dim yellow]{line}[/]")
            except Exception:
                pass

        # --- HEVC video diagnostics ---
        console.print("  [bold cyan]═══ HEVC Video Diagnostics ═══[/]")
        if tmp_video.exists():
            vid_playable, vid_errors = self._diagnostic_play(tmp_video)
            vid_probe_data, vid_probe_errors = self._diagnostic_probe(tmp_video)
        else:
            vid_playable = False
            vid_errors = ["File does not exist"]
            vid_probe_data = None
            vid_probe_errors = ["File does not exist"]

        console.print(f"    Playable: {'[green]✓[/]' if vid_playable else '[bold red]✗[/]'}  |  "
                      f"Probe: {'[green]✓[/]' if vid_probe_data else '[bold red]✗[/]'}")
        if vid_errors:
            console.print(f"    [yellow]Decode warnings/errors ({len(vid_errors)}):[/]")
            for err in vid_errors[:8]:
                console.print(f"      [dim yellow]{err}[/]")
            if len(vid_errors) > 8:
                console.print(f"      [dim yellow]... and {len(vid_errors)-8} more[/]")
        if vid_probe_errors:
            console.print(f"    [yellow]Probe warnings/errors ({len(vid_probe_errors)}):[/]")
            for err in vid_probe_errors[:8]:
                console.print(f"      [dim yellow]{err}[/]")
            if len(vid_probe_errors) > 8:
                console.print(f"      [dim yellow]... and {len(vid_probe_errors)-8} more[/]")

        if vid_probe_data:
            for s_idx, stream in enumerate(vid_probe_data.get('streams', [])):
                codec_type = stream.get('codec_type', '?')
                codec_name = stream.get('codec_name', '?')
                dur = stream.get('duration', '?')
                nb_frames = stream.get('nb_frames', '?')
                issues = []
                if stream.get('codec_tag') == '0x0000':
                    issues.append("Zero codec tag")
                if dur == 'N/A' or dur == 0:
                    issues.append("Duration unreadable")
                if codec_type == 'video' and nb_frames == 'N/A':
                    issues.append("Frame count unreadable")
                issue_str = f" [yellow]{'; '.join(issues)}[/]" if issues else ""
                console.print(f"    Stream {s_idx} ({codec_type}/{codec_name}): dur={dur}s frames={nb_frames}{issue_str}")

        # --- GATE-1/2 failure analysis ---
        console.print("  [bold cyan]═══ GATE-1/2 Failure Analysis ═══[/]")
        if vid_delta > 0:
            src_v_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
            src_vs = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
            if src_vs:
                try:
                    src_v_dur = float(src_vs[0].get('duration') or src_v_dur)
                except (ValueError, TypeError):
                    pass
            out_v_dur = 0.0
            if vid_probe_data:
                out_v_dur = float(vid_probe_data.get('format', {}).get('duration', 0) or 0)
                out_vs = [s for s in vid_probe_data.get('streams', []) if s.get('codec_type') == 'video']
                if out_vs:
                    try:
                        out_v_dur = float(out_vs[0].get('duration') or out_v_dur)
                    except (ValueError, TypeError):
                        pass
            console.print(f"    [bold yellow]GATE-1 (Video Duration):[/]")
            console.print(f"      Source: {src_v_dur:.3f}s → Output: {out_v_dur:.3f}s → Δ={vid_delta:.3f}s")
            if vid_delta > 30:
                console.print(f"      [red]→ Likely: truncated encode, stream corruption, or container rewrite failure[/]")
            elif vid_delta > 5:
                console.print(f"      [yellow]→ Likely: missing frames, timestamp discontinuity, or seek table error[/]")
            else:
                console.print(f"      [yellow]→ Likely: encoder priming frames, rounding, or minor timestamp shift[/]")

        if aud_delta > 0:
            src_a_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
            src_as = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
            if src_as:
                try:
                    src_a_dur = float(src_as[0].get('duration') or src_a_dur)
                except (ValueError, TypeError):
                    pass
            console.print(f"    [bold yellow]GATE-2 (Audio Duration):[/]")
            console.print(f"      Source audio: {src_a_dur:.3f}s → Δ={aud_delta:.3f}s")
            if aud_delta > 30:
                console.print(f"      [red]→ Likely: audio truncated, demuxer failure, or copy codec mismatch[/]")
            elif aud_delta > 5:
                console.print(f"      [yellow]→ Likely: audio priming/padding removal, container duration rewrite[/]")
            else:
                console.print(f"      [yellow]→ Likely: encoder delay compensation (priming samples), rounding[/]")

    # -----------------------------------------------------------------------
    # GATE-7: Mismatch preservation handler
    #
    # Sequence: GATE-7 invoked → GATE-3 → GATE-4 → GATE-5 → GATE-6
    #
    # If ALL pass → save to mismatched_vault, source KEPT
    #   Δ < 2s → save HEVC+Copy muxed
    #   Δ ≥ 2s → save video + audio SEPARATELY
    #
    # If ANY fail → processed DELETED, source kept
    # -----------------------------------------------------------------------
    def _gate7_handle(self, src: Path, src_meta: Dict[str, Any],
                      tmp_video: Path, audio_streams: List[Dict[str, Any]],
                      mismatch_info: Dict[str, float],
                      subtitle_count: int, attachment_count: int,
                      encoder_tag: str) -> None:

        max_delta = mismatch_info["max_delta"]

        # --- Display full diagnostics ---
        self._gate7_diagnostics(src, src_meta, tmp_video, mismatch_info)

        # ==============================================================
        # GATE-7 sequence: GATE-3 → GATE-4 → GATE-5
        # We check these on the HEVC+Copy candidate
        # ==============================================================

        # Generate HEVC+Copy candidate for GATE-7
        console.print(f"  [cyan]GATE-7: Generating HEVC+Copy candidate for remaining gate checks...[/]")

        if audio_streams:
            copy_combo = tuple([0] * len(audio_streams))
            cand = self._mux_candidate(
                src, tmp_video, audio_streams, copy_combo,
                label="gate7_hevc_copy",
                subtitle_count=subtitle_count,
                attachment_count=attachment_count
            )
        else:
            cand = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_gate7_hevc_copy.mkv"
            shutil.copyfile(str(tmp_video), str(cand))

        if cand is None or not cand.exists():
            console.print("  [bold red]GATE-7: Failed to mux HEVC+Copy candidate. Processed DELETED.[/]")
            self.session_summary.append(
                (src.name[:25], f"{encoder_tag}/GATE7", "[red]Mux Fail[/]",
                 f"Δ={max_delta:.2f}s | mux failed | source kept")
            )
            return

        # --- GATE-3 → GATE-4 → GATE-5 ---
        console.print(f"  [cyan]GATE-7 sequence: Running GATE-3 → GATE-4 → GATE-5...[/]")

        passed_345, gate_failed_345, detail_345, black_probable_345 = self._check_gate345(src, src_meta, cand)

        if black_probable_345:
            console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {detail_345}")
            # Still continue GATE-7 — just note it. File goes to mismatched_vault, not black_probable.

        if not passed_345 and not black_probable_345:
            console.print(f"  [bold red]{gate_failed_345} FAIL:[/] {detail_345}")
            console.print(f"  [bold red]GATE-7: Hard gate failed in mismatch path. Processed DELETED. Source kept.[/]")
            cand.unlink()
            self.session_summary.append(
                (src.name[:25], f"{encoder_tag}/GATE7", "[red]Gate Fail[/]",
                 f"Δ={max_delta:.2f}s | {gate_failed_345}: {detail_345[:60]} | source kept")
            )
            return

        # ==============================================================
        # GATE-6: Size comparison
        # ==============================================================
        console.print(f"  [cyan]GATE-7 sequence: Running GATE-6 size comparison...[/]")

        src_size = src.stat().st_size
        cand_size = cand.stat().st_size

        src_sz_str = _format_size(src_size)
        cand_sz_str = _format_size(cand_size)
        console.print(f"  [dim]Sizes → Source: {src_sz_str} | HEVC+Copy: {cand_sz_str}[/]")

        gate6_pass, gate6_detail = self._gate6_check(src_size, cand_size, "HEVC+Copy")

        if not gate6_pass:
            console.print(f"  [bold red]{gate6_detail}[/]")
            console.print(f"  [bold red]GATE-7: Size check FAILED. Processed DELETED. Source kept.[/]")
            cand.unlink()
            self.session_summary.append(
                (src.name[:25], f"{encoder_tag}/GATE7", "[red]Wasteful[/]",
                 f"Δ={max_delta:.2f}s | GATE-6 fail | {cand_sz_str} not smaller | source kept")
            )
            return

        # ==============================================================
        # ALL GATES PASSED in GATE-7 path → SAVE to mismatched_vault
        # Source is ALWAYS KEPT (delete_source = False for this one)
        # ==============================================================

        console.print(f"  [green]{gate6_detail}[/]")
        console.print(f"  [bold green]GATE-7: ALL remaining gates passed. Saving to mismatched_vault/[/]")

        # Diagnose the final muxed result
        mux_playable, mux_errors = self._diagnostic_play(cand)
        if mux_errors:
            console.print(f"    [yellow]Muxed output warnings ({len(mux_errors)}):[/]")
            for err in mux_errors[:5]:
                console.print(f"      [dim yellow]{err}[/]")

        # --- Δ < 2s: save HEVC+Copy muxed ---
        if max_delta < _GATE7_MISMATCH_THRESHOLD:
            dest = self._get_mismatch_path(src, "gate7_mismatch_hevc_copy")
            try:
                shutil.move(str(cand), str(dest))
            except OSError:
                shutil.copyfile(str(cand), str(dest))
                cand.unlink()

            dest_size = dest.stat().st_size
            ratio = (dest_size / src_size * 100) if src_size > 0 else 0
            savings_pct = ((src_size - dest_size) / src_size * 100) if src_size > 0 else 0

            console.print(
                f"  [bold green]GATE-7 WIN (Δ={max_delta:.2f}s < {_GATE7_MISMATCH_THRESHOLD}s):[/] Saved as muxed HEVC+Copy\n"
                f"  [dim]{src_sz_str} → {_format_size(dest_size)} (-{savings_pct:.1f}%) | {dest.name}[/]\n"
                f"  [bold blue]Source RETAINED[/] (both source + mismatched version kept)"
            )
            self.session_summary.append(
                (src.name[:25], f"{encoder_tag}/GATE7", "[green]Win (kept src)[/]",
                 f"Δ={max_delta:.2f}s | {src_sz_str}→{_format_size(dest_size)} (-{savings_pct:.1f}%) | muxed")
            )

        # --- Δ ≥ 2s: save video + audio SEPARATELY ---
        else:
            saved_video = False
            saved_audio = False

            # Video-only
            vid_dest = self._get_mismatch_path(src, "gate7_mismatch_video_only")
            if vid_dest.suffix != '.mkv':
                vid_dest = vid_dest.with_suffix('.mkv')
            try:
                shutil.copyfile(str(tmp_video), str(vid_dest))
                saved_video = True
                console.print(f"  [green]Video-only saved:[/] {vid_dest.name} ({_format_size(vid_dest.stat().st_size)})")
            except OSError as e:
                console.print(f"  [bold red]Failed to save video-only:[/] {e}")

            # Audio-only from source
            if audio_streams:
                aud_dest = self._get_mismatch_path(src, "gate7_mismatch_audio_only")
                aud_dest = aud_dest.with_suffix('.mka')
                aud_tmp = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_audio_only.mka"
                cmd = [_FFMPEG, "-y", "-v", "error", "-i", str(src)]
                for a_idx in range(len(audio_streams)):
                    cmd.extend(["-map", f"0:a:{a_idx}", f"-c:a:{a_idx}", "copy"])
                cmd.append(str(aud_tmp))
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode != 0:
                        console.print(f"  [bold red]Audio extraction failed:[/] {result.stderr.strip()[:100]}")
                        if aud_tmp.exists():
                            aud_tmp.unlink()
                    else:
                        shutil.move(str(aud_tmp), str(aud_dest))
                        saved_audio = True
                        console.print(f"  [green]Audio-only saved:[/] {aud_dest.name} ({_format_size(aud_dest.stat().st_size)})")
                except Exception as e:
                    console.print(f"  [bold red]Audio extraction error:[/] {e}")
                    if aud_tmp.exists():
                        aud_tmp.unlink()
            else:
                saved_audio = True

            # Delete the muxed candidate since we saved separately
            if cand.exists():
                cand.unlink()

            savings_pct = ((src_size - cand_size) / src_size * 100) if src_size > 0 else 0

            if saved_video and saved_audio:
                console.print(
                    f"  [bold green]GATE-7 WIN (Δ={max_delta:.2f}s ≥ {_GATE7_MISMATCH_THRESHOLD}s):[/] Saved video + audio SEPARATELY\n"
                    f"  [dim]{src_sz_str} → {_format_size(cand_size)} (-{savings_pct:.1f}%) | split vid+aud[/]\n"
                    f"  [bold blue]Source RETAINED[/] (both source + mismatched version kept)"
                )
                self.session_summary.append(
                    (src.name[:25], f"{encoder_tag}/GATE7", "[green]Win (kept src)[/]",
                     f"Δ={max_delta:.2f}s | {src_sz_str}→{_format_size(cand_size)} (-{savings_pct:.1f}%) | split")
                )
            else:
                console.print(f"  [bold red]GATE-7: Partial save failure (vid={'✓' if saved_video else '✗'} aud={'✓' if saved_audio else '✗'})[/]")
                self.session_summary.append(
                    (src.name[:25], f"{encoder_tag}/GATE7", "[red]Partial Fail[/]",
                     f"Δ={max_delta:.2f}s | vid={'✓' if saved_video else '✗'} aud={'✓' if saved_audio else '✗'}")
                )

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    def run(self) -> None:
        source_files: List[Path] = []
        skip_dirs = {self.vault_dir, self.archive_dir, self.bypass_dir,
                     self.black_prob_dir, self.mismatch_dir}

        for root, dirs, files in os.walk(self.input_dir):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if root_path / d not in skip_dirs]
            for fname in files:
                fp = root_path / fname
                if fp.suffix.lower() in self.media_extensions:
                    source_files.append(fp)

        if not source_files:
            console.print("[yellow]No supported media found in workspace tree.[/]")
            return

        gpu_status = f"VAAPI via {self.gpu_device}" if self.gpu_device else "CPU-only (no GPU backend detected)"
        console.print(Panel(
            f"[bold green]▶ HV_OP V2.9.9-G7 — TMPFS + V3.2 OPUS + TREE VAULT + GATE-7[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n"
            f"[dim]Video backend: {gpu_status}[/]\n"
            f"[dim]RAM Workspace: {_TMFS_DIR}[/]\n"
            f"[dim]GATE-6 Standard: ≥{_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR ≥{_MIN_ABSOLUTE_SAVINGS_MB}MB savings[/]\n"
            f"[dim]GATE-6 Micro (<{_MICRO_FILE_THRESHOLD_MB}MB): ≥{_MICRO_FILE_PERCENTAGE_SAVINGS*100:.0f}% savings[/]\n"
            f"[bold yellow]GATE-7 Sequence: GATE-7 → GATE-3 → GATE-4 → GATE-5 → GATE-6[/]\n"
            f"[bold yellow]GATE-7 Mismatch threshold: {_GATE7_MISMATCH_THRESHOLD}s[/]\n"
            f"[dim]Files found: {len(source_files)}[/]",
            border_style="green"
        ))

        for idx, f in enumerate(source_files):
            console.print(Panel(
                f"[bold magenta][{idx+1}/{len(source_files)}][/] [bold white]{f.name}[/]\n[dim]{f.parent}[/]",
                border_style="magenta"
            ))

            meta = self._probe(f)
            if not meta or 'streams' not in meta:
                console.print(f"  [red]Unreadable container. Skipping.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Unreadable container"))
                continue

            src_size = f.stat().st_size
            if src_size == 0:
                console.print(f"  [red]Zero byte file. Skipping.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Zero bytes"))
                continue

            if not self._is_playable(f):
                console.print(f"  [red]Source unplayable pre-flight. Skipping.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Source unplayable"))
                continue

            video_streams = [s for s in meta['streams'] if s.get('codec_type') == 'video']
            audio_streams = [s for s in meta['streams'] if s.get('codec_type') == 'audio']
            subtitle_streams = [s for s in meta['streams'] if s.get('codec_type') == 'subtitle']
            attachment_streams = [s for s in meta['streams'] if s.get('codec_type') == 'attachment']

            if not video_streams:
                console.print(f"  [yellow]No video stream — routing to bypass_file/[/]")
                try:
                    dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                    shutil.move(str(f), str(self.bypass_dir / dest_name))
                    self.session_summary.append((f.name[:25], "NO-VID", "[yellow]Bypassed[/]", "No video stream"))
                except Exception as e:
                    self.session_summary.append((f.name[:25], "NO-VID", "[red]Bypass Fail[/]", str(e)[:80]))
                continue

            v0 = video_streams[0]
            src_sz_display = _format_size(src_size)
            console.print(
                f"  [dim]Codec: {v0.get('codec_name','?')} | "
                f"{v0.get('width','?')}x{v0.get('height','?')} | "
                f"Dur: {meta.get('format',{}).get('duration','?')}s | "
                f"Size: {src_sz_display} | "
                f"Audio: {len(audio_streams)} | Subs: {len(subtitle_streams)} | "
                f"Attach: {len(attachment_streams)}[/]"
            )

            if v0.get('codec_name', '').lower() == 'hevc':
                console.print(f"  [yellow]Already HEVC — routing to bypass_file/[/]")
                try:
                    dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                    shutil.move(str(f), str(self.bypass_dir / dest_name))
                    self.session_summary.append((f.name[:25], "HEVC", "[yellow]Bypassed[/]", "Moved to bypass_file/"))
                except Exception as e:
                    self.session_summary.append((f.name[:25], "HEVC", "[red]Bypass Fail[/]", str(e)[:80]))
                continue

            # ================================================================
            # Stage 1: Encode HEVC video
            # ================================================================
            t0 = time.time()
            if self.gpu_device:
                console.print("  [cyan]Stage 1: Encoding HEVC video (VAAPI GPU) to RAM...[/]")
                tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=False)
                use_fallback = False
                if tmp_video is None:
                    console.print("  [yellow]GPU encode failed. Falling back to CPU (libx265) in RAM...[/]")
                    tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=True)
                    use_fallback = True
            else:
                console.print("  [cyan]Stage 1: Encoding HEVC video (CPU libx265) to RAM...[/]")
                tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=True)
                use_fallback = True

            if tmp_video is None:
                console.print("  [bold red]HEVC encode crashed on both pipelines. Source untouched.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Crash[/]", "HEVC encode failure"))
                continue

            encoder_tag = "CPU_x265" if use_fallback else "GPU_VAAPI"
            console.print(f"  [dim]Video encoded in {time.time()-t0:.1f}s via {encoder_tag}[/]")

            # ================================================================
            # Stage 2: Generate dual audio candidates
            # ================================================================
            console.print("  [cyan]Stage 2: Generating dual audio candidates (Opus vs Copy)...[/]")

            cand_opus, cand_copy = self._mux_audio_candidates(
                f, tmp_video, audio_streams,
                subtitle_count=len(subtitle_streams),
                attachment_count=len(attachment_streams)
            )

            if not cand_opus and not cand_copy:
                console.print("  [bold red]Both audio mux candidates failed. Source untouched.[/]")
                self.session_summary.append((f.name[:25], f"{encoder_tag}/mux", "[red]Crash[/]", "All mux failed"))
                if tmp_video.exists():
                    tmp_video.unlink()
                continue

            # ================================================================
            # Stage 3: GATE-1 → GATE-2 → GATE-3 → GATE-4 → GATE-5
            # ================================================================
            console.print("  [cyan]Stage 3: Running GATE-1 → GATE-2 → GATE-3 → GATE-4 → GATE-5...[/]")

            valid_candidates = []     # Passed ALL gates including GATE-1/2
            mismatch_candidates = []  # Passed GATE-3/4/5 but failed GATE-1/2
            black_probable_flag = False
            bp_cand = None
            bp_detail = ""
            bp_tag = ""

            # --- Verify Opus Candidate ---
            if cand_opus and cand_opus.exists():
                passed, gate_failed, detail, black_probable, mismatch_info = self._verify_early_gates(f, meta, cand_opus)

                if black_probable:
                    black_probable_flag = True
                    bp_cand = cand_opus
                    bp_detail = detail
                    bp_tag = "all-opus"
                elif mismatch_info is not None and passed:
                    # GATE-1/2 failed but GATE-3/4/5 passed → GATE-7 candidate
                    mismatch_candidates.append((cand_opus, "all-opus", cand_opus.stat().st_size, mismatch_info))
                elif mismatch_info is not None and not passed:
                    # Both GATE-1/2 AND hard gates failed → discard
                    console.print(f"  [bold red]OPUS {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_opus.unlink()
                elif passed:
                    valid_candidates.append((cand_opus, "all-opus", cand_opus.stat().st_size))
                else:
                    console.print(f"  [bold red]OPUS {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_opus.unlink()

            # --- Verify Copy Candidate ---
            if not black_probable_flag and cand_copy and cand_copy.exists():
                passed, gate_failed, detail, black_probable, mismatch_info = self._verify_early_gates(f, meta, cand_copy)

                if black_probable:
                    black_probable_flag = True
                    bp_cand = cand_copy
                    bp_detail = detail
                    bp_tag = "all-copy"
                elif mismatch_info is not None and passed:
                    mismatch_candidates.append((cand_copy, "all-copy", cand_copy.stat().st_size, mismatch_info))
                elif mismatch_info is not None and not passed:
                    console.print(f"  [bold red]COPY {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_copy.unlink()
                elif passed:
                    valid_candidates.append((cand_copy, "all-copy", cand_copy.stat().st_size))
                else:
                    console.print(f"  [bold red]COPY {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_copy.unlink()

            # --- Handle GATE-5 Black Probable ---
            if black_probable_flag:
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {bp_detail} -> moving to hevc_black_probable/")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(bp_cand), str(bp_dest))
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{bp_tag}", "[yellow]Black Prob[/]", bp_detail))
                if cand_opus and cand_opus.exists() and cand_opus != bp_cand:
                    cand_opus.unlink()
                if cand_copy and cand_copy.exists() and cand_copy != bp_cand:
                    cand_copy.unlink()
                if tmp_video.exists():
                    tmp_video.unlink()
                continue

            # ============================================================
            # GATE-7 PATH: No clean candidates, only mismatch candidates
            # Sequence: GATE-7 invoked → GATE-3 → GATE-4 → GATE-5 → GATE-6
            # ============================================================
            if not valid_candidates and mismatch_candidates:
                console.print("  [bold yellow]GATE-1/2 FAILED on all candidates → GATE-7 path activated[/]")
                console.print("  [bold yellow]GATE-7 sequence: GATE-7 → GATE-3 → GATE-4 → GATE-5 → GATE-6[/]")

                # Clean up candidates — GATE-7 generates its own HEVC+Copy from tmp_video
                for cand, tag, sz, mi in mismatch_candidates:
                    if cand.exists():
                        cand.unlink()

                # Use mismatch_info from copy candidate preferentially
                mismatch_info = None
                for cand, tag, sz, mi in mismatch_candidates:
                    if tag == "all-copy":
                        mismatch_info = mi
                        break
                if mismatch_info is None:
                    mismatch_info = mismatch_candidates[0][3]

                # Invoke GATE-7 handler
                self._gate7_handle(
                    src=f,
                    src_meta=meta,
                    tmp_video=tmp_video,
                    audio_streams=audio_streams,
                    mismatch_info=mismatch_info,
                    subtitle_count=len(subtitle_streams),
                    attachment_count=len(attachment_streams),
                    encoder_tag=encoder_tag
                )

                # Clean up tmp_video
                if tmp_video.exists():
                    tmp_video.unlink()

                # Source is RETAINED — GATE-7 never deletes source
                continue

            # Clean up mismatch_candidates if we have clean ones
            for cand, tag, sz, mi in mismatch_candidates:
                if cand.exists():
                    cand.unlink()

            # Clean up tmp_video
            if tmp_video.exists():
                tmp_video.unlink()

            # ============================================================
            # NORMAL PATH: At least one clean candidate exists
            # ============================================================
            if not valid_candidates:
                console.print("  [bold red]No candidates passed early gates. Source untouched.[/]")
                self.session_summary.append((f.name[:25], encoder_tag, "[red]Crash[/]", "Early gates failed"))
                continue

            # ================================================================
            # Stage 4: GATE-6 3-way size comparison (normal path)
            # ================================================================
            console.print("  [cyan]Stage 4: Running GATE-6 3-way size comparison...[/]")

            best_cand = None
            best_tag = ""
            best_size = 0

            if len(valid_candidates) == 2:
                cand_dict = {tag: (cand, sz) for cand, tag, sz in valid_candidates}
                opus_cand, opus_sz = cand_dict["all-opus"]
                copy_cand, copy_sz = cand_dict["all-copy"]

                adjusted_opus_sz = 1.1 * opus_sz
                adj_opus_sz_int = int(adjusted_opus_sz)

                src_sz_str = _format_size(src_size)
                opus_sz_str = _format_size(opus_sz)
                copy_sz_str = _format_size(copy_sz)
                adj_opus_sz_str = _format_size(adj_opus_sz_int)

                console.print(f"  [dim]Sizes -> Source: {src_sz_str} | Opus: {opus_sz_str} | Copy: {copy_sz_str}[/]")
                console.print(f"  [dim]Comparison: 1.1 * Opus ({adj_opus_sz_str}) v/s Copy ({copy_sz_str})[/]")

                if adjusted_opus_sz <= copy_sz:
                    best_cand = opus_cand
                    best_tag = "all-opus"
                    copy_cand.unlink()
                    console.print(f"  [green]Opus automatically wins (1.1 * Opus <= Copy).[/]")
                else:
                    best_cand = copy_cand
                    best_tag = "all-copy"
                    opus_cand.unlink()
                    console.print(f"  [yellow]Copy wins (1.1 * Opus > Copy, so Opus fails comparison).[/]")
            else:
                best_cand, best_tag, _ = valid_candidates[0]
                console.print(f"  [dim]Only one candidate survived early gates: {best_tag}[/]")

            best_size = best_cand.stat().st_size

            # GATE-6
            gate6_pass, gate6_detail = self._gate6_check(src_size, best_size, best_tag)

            if not gate6_pass:
                console.print(f"  [bold red]{gate6_detail}[/]")
                if best_cand.exists():
                    best_cand.unlink()
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}", "[red]GATE-6 Fail[/]", gate6_detail[:80]))
                continue

            console.print(f"  [green]{gate6_detail}[/]")

            staged_output = self.prcs_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_staged.mkv"
            shutil.move(str(best_cand), str(staged_output))

            potato = self._potato_tier(src_size, staged_output.stat().st_size)
            recipe = f"{encoder_tag} / {best_tag}"

            final_output = self._get_vault_path(f)
            try:
                shutil.move(str(staged_output), str(final_output))
            except OSError:
                shutil.copyfile(str(staged_output), str(final_output))
                staged_output.unlink()

            console.print(f"  [bold green]All gates passed.[/] Promoted to vault tree. Class: {potato}")

            if SOURCE_REMOVAL:
                _black_hole(f)
                console.print(f"  [bold red]Source sent to black hole[/] (SOURCE_REMOVAL=True)")
            else:
                archive_dest = self.archive_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                shutil.move(str(f), str(archive_dest))
                console.print(f"  [bold blue]Source moved to prcs_cmpl/[/] (SOURCE_REMOVAL=False)")

            self.session_summary.append((f.name[:25], recipe, "[green]Success[/]", potato))

        # ====================================================================
        # Session Summary Table
        # ====================================================================
        table = Table(
            title="HV_OP V2.9.9-G7 — tmpfs + V3.2 Opus + Tree Vault + GATE-7 Matrix",
            box=box.DOUBLE_EDGE, expand=True
        )
        table.add_column("Asset", ratio=3, style="cyan")
        table.add_column("Pipeline", ratio=3, justify="center")
        table.add_column("Result", ratio=2)
        table.add_column("Savings / Notes", ratio=4, style="dim")
        for row in self.session_summary:
            table.add_row(*row)
            table.add_section()
        console.print(table)


if __name__ == "__main__":
    working_path = input("Enter target workspace directory (or hit Enter for current): ").strip()
    output_path = input("Enter output directory (or hit Enter for same as source): ").strip()
    engine = AutomatedHvOpEngine(
        working_path if working_path else ".",
        output_path
    )
    engine.run()
