#!/usr/bin/env python3
"""
HV_OP - V2.9.10-G7: tmpfs Pipeline + Dual Audio + Tree Vault
                     + GATE-7 Dynamic Mismatch Triage + JSON Log
------------------------------------------------------------------------------------
Gate Sequence:

  CLEAN CANDIDATE:   GATE-1 → GATE-2 → GATE-3 → GATE-4 → GATE-5 → GATE-6
  MISMATCH CANDIDATE: GATE-1/2 fail → GATE-7 (threshold check)
                     → GATE-3 → GATE-4 → GATE-5 → GATE-6

  GATE-6 Dual-Candidate Mode (clean vs mismatch):
    Source vs clean (1.1× Opus penalty if Opus) vs mismatch
    ├── Clean wins:   source removed (per SOURCE_REMOVAL), mismatch dropped
    └── Mismatch wins: clean dropped, source KEPT, mismatch → mismatch_vault

  Both-mismatch case: winner → mismatch_vault, source KEPT.

  Dynamic safe-threshold by source duration:
    < 1 min  → 2s
    < 10 min → 4s
    ≥ 10 min → 10s
    Δ > threshold → _broken suffix added to filename

  Output: video + audio always saved as ONE muxed file.
  Log:    result_log.json with per-file status + reason.
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
import selectors
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

try:
    import ujson as json
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
_BLACK_HOLE_DIR = Path('/home/pnsi/system_clean/to_be_removed/')
_TMFS_DIR = Path('/dev/shm/hv_op_workspace')
_USER = getpass.getuser()
_FFMPEG_DIR = Path(f'/home/{_USER}/ffmpeg/bin')
SOURCE_REMOVAL = True

# GATE-6 Dynamic Savings Rules
_MIN_ABSOLUTE_SAVINGS_MB = 40
_MIN_PERCENTAGE_SAVINGS = 0.10
_MICRO_FILE_THRESHOLD_MB = 5.0
_MICRO_FILE_PERCENTAGE_SAVINGS = 0.20

# Opus penalty: Opus has higher seek/container overhead than copied audio
_OPUS_OVERHEAD_PENALTY = 1.10

# V3.2 Opus 6-tier psychoacoustic ladder (FIXED: no voip, no 12k, all 20ms)
OPUS_LADDER = [
    {"limit": 64,           "br": "32k",  "frame": "20", "app": "audio"},
    {"limit": 96,           "br": "64k",  "frame": "20", "app": "audio"},
    {"limit": 160,          "br": "96k",  "frame": "20", "app": "audio"},
    {"limit": 256,          "br": "128k", "frame": "20", "app": "audio"},
    {"limit": 448,          "br": "192k", "frame": "20", "app": "audio"},
    {"limit": float('inf'), "br": "256k", "frame": "20", "app": "audio"},
]

# Per-session tmpfs tracking for safe concurrent runs
_ALL_SESSION_DIRS: List[Path] = []


def _cleanup_tmpfs_exit():
    global _ALL_SESSION_DIRS
    for d in _ALL_SESSION_DIRS:
        if d and d.exists():
            shutil.rmtree(d, ignore_errors=True)
    _ALL_SESSION_DIRS.clear()


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
    """Dynamically format size to KB or MB (FIXED: uses float division)."""
    if size_bytes < 1048576:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / 1048576:.1f}MB"


def _black_hole(path: Path) -> None:
    """Drop SOURCE file into black hole dir."""
    if not path.exists():
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

        try:
            _BLACK_HOLE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            console.print(f"[red]Cannot create black hole dir: {e}[/]")

        # Clean stale sessions (older than 24h)
        try:
            for old in _TMFS_DIR.glob("sess_*"):
                if old.stat().st_mtime < time.time() - 86400:
                    shutil.rmtree(old, ignore_errors=True)
        except Exception:
            pass

        self.session_id = uuid.uuid4().hex[:8]
        self.prcs_dir = _TMFS_DIR / f"sess_{self.session_id}"
        self.prcs_dir.mkdir(parents=True, exist_ok=True)
        _ALL_SESSION_DIRS.append(self.prcs_dir)

        self.media_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.webm',
            '.flv', '.wmv', '.m4v', '.ts', '.mts', '.3gp'
        }
        self.session_summary: List[Tuple[str, str, str, str]] = []

        # Caches
        self._playable_cache: Dict[str, bool] = {}
        self._probe_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._black_ratio_cache: Dict[str, float] = {}

        # Result log
        self.result_log: List[Dict[str, Any]] = []
        self._log_written = False
        self.session_start = time.strftime("%Y-%m-%d %H:%M:%S")
        atexit.register(self._write_result_log)

        self.gpu_device = _detect_gpu_backend()
        if self.gpu_device:
            console.print(f"[bold green]GPU backend detected:[/] VAAPI via {self.gpu_device}")
        else:
            console.print("[yellow]No usable VAAPI GPU backend found — running CPU-only (libx265).[/]")

    # -----------------------------------------------------------------------
    # Result Log
    # -----------------------------------------------------------------------
    def _write_result_log(self) -> None:
        """Write result_log.json with per-file status and reasons."""
        if getattr(self, '_log_written', False):
            return
        self._log_written = True
        
        log_path = self.output_dir / "result_log.json"
        try:
            log_data = {
                "session_id": self.session_id,
                "session_start": self.session_start,
                "session_end": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": self.result_log,
                "summary": {
                    "total": len(self.result_log),
                    "success": sum(1 for r in self.result_log if r["status"] == "success"),
                    "mismatch_clean": sum(1 for r in self.result_log if r["status"] == "mismatch_clean"),
                    "mismatch_broken": sum(1 for r in self.result_log if r["status"] == "mismatch_broken"),
                    "skipped": sum(1 for r in self.result_log if r["status"].startswith("skip")),
                    "failed": sum(1 for r in self.result_log if r["status"] == "fail"),
                }
            }
            with open(log_path, 'w') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            console.print(f"\n[green]Result log saved:[/] {log_path}")
        except Exception as e:
            console.print(f"[red]Failed to write result log: {e}[/]")

    def _log_entry(self, file_path: Path, status: str, reason: str,
                   **kwargs) -> None:
        """Append a result entry to the session log."""
        entry: Dict[str, Any] = {
            "file": file_path.name,
            "path": str(file_path),
            "status": status,
            "reason": reason,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        entry.update(kwargs)
        self.result_log.append(entry)

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
        # FIXED: Always .mkv for vault outputs since we mux to MKV container
        clean = dest_dir / f"{rel_path.stem}_vault.mkv"
        if not clean.exists():
            return clean
        return dest_dir / f"{rel_path.stem}_{uuid.uuid4().hex[:8]}_vault.mkv"

    def _get_mismatch_path(self, src: Path, suffix: str) -> Path:
        try:
            rel_path = src.relative_to(self.input_dir)
        except ValueError:
            rel_path = Path(src.name)
        dest_dir = self.mismatch_dir / rel_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean = dest_dir / f"{rel_path.stem}{suffix}.mkv"
        if not clean.exists():
            return clean
        return dest_dir / f"{rel_path.stem}_{uuid.uuid4().hex[:8]}{suffix}.mkv"

    # -----------------------------------------------------------------------
    # Probe & Playable 
    # -----------------------------------------------------------------------
    def _probe(self, path: Path) -> Optional[Dict[str, Any]]:
        key = str(path)
        if key in self._probe_cache:
            return self._probe_cache[key]
        try:
            cmd = [_FFPROBE, "-v", "quiet", "-print_format", "json",
                   "-show_streams", "-show_format", str(path)]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            result = json.loads(res.stdout)
            self._probe_cache[key] = result
            return result
        except Exception:
            self._probe_cache[key] = None
            return None

    def _is_playable(self, path: Path) -> bool:
        """Check if file is playable (FIXED: dynamic timeout based on duration)."""
        key = str(path)
        if key in self._playable_cache:
            return self._playable_cache[key]
        try:
            meta = self._probe(path)
            duration = 0.0
            if meta:
                duration = float(meta.get('format', {}).get('duration', 0) or 0)
            # Allow ~8x realtime decode; minimum 60s, max 600s
            timeout = max(60, min(600, int(duration / 8) + 30))
            
            subprocess.run([_FFMPEG, "-v", "error", "-i", str(path),
                            "-f", "null", "-"],
                           check=True, capture_output=True, timeout=timeout)
            self._playable_cache[key] = True
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            self._playable_cache[key] = False
            return False

    # -----------------------------------------------------------------------
    # GATE-7 Diagnostic helpers
    # -----------------------------------------------------------------------
    def _diagnostic_play(self, path: Path) -> Tuple[bool, List[str]]:
        try:
            meta = self._probe(path)
            duration = 0.0
            if meta:
                duration = float(meta.get('format', {}).get('duration', 0) or 0)
            timeout = max(60, min(600, int(duration / 8) + 30))
            
            result = subprocess.run(
                [_FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, timeout=timeout
            )
            errors = [l.strip() for l in result.stderr.splitlines() if l.strip()]
            if result.returncode == 0:
                return True, errors
            return False, errors
        except subprocess.TimeoutExpired:
            return False, ["Decode timed out"]
        except Exception as e:
            return False, [f"Exception during decode: {e}"]

    def _diagnostic_probe(self, path: Path) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        errors: List[str] = []
        try:
            cmd = [_FFPROBE, "-v", "error", "-print_format", "json",
                   "-show_streams", "-show_format", str(path)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            errors = [l.strip() for l in result.stderr.splitlines() if l.strip()]
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

    def _display_errors(self, label: str, errors: List[str]) -> None:
        if not errors:
            return
        console.print(f"    [yellow]{label} ({len(errors)}):[/]")
        for err in errors[:8]:
            console.print(f"      [dim yellow]{err}[/]")
        if len(errors) > 8:
            console.print(f"      [dim yellow]... and {len(errors)-8} more[/]")

    def _display_stream_health(self, streams: List[Dict[str, Any]]) -> None:
        for s_idx, stream in enumerate(streams):
            codec_type = stream.get('codec_type', '?')
            codec_name = stream.get('codec_name', '?')
            dur = stream.get('duration', '?')
            nb_frames = stream.get('nb_frames', '?')
            issues = []
            if stream.get('codec_tag') == '0x0000':
                issues.append("Zero codec tag — possible header corruption")
            if dur in ('N/A', 0, '0'):
                issues.append("Duration unreadable — possible truncated stream")
            if codec_type == 'video' and nb_frames == 'N/A':
                issues.append("Frame count unreadable — possible index corruption")
            issue_str = f" [yellow]{'; '.join(issues)}[/]" if issues else ""
            console.print(f"    Stream {s_idx} ({codec_type}/{codec_name}): "
                          f"dur={dur}s frames={nb_frames}{issue_str}")

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
    # Opus tier selection (FIXED: no voip, channel-aware)
    # -----------------------------------------------------------------------
    def _pick_opus_tier(self, br_kbps: float, channels: int = 2) -> Dict[str, str]:
        """Select Opus tier by source bitrate. Boost one tier for surround."""
        base_idx = 0
        for idx, tier in enumerate(OPUS_LADDER):
            if br_kbps <= tier["limit"]:
                base_idx = idx
                break
        else:
            base_idx = len(OPUS_LADDER) - 1
        if channels >= 6 and base_idx < len(OPUS_LADDER) - 1:
            return OPUS_LADDER[base_idx + 1]
        return OPUS_LADDER[base_idx]

    # -----------------------------------------------------------------------
    # Mismatch threshold (NEW: dynamic by duration)
    # -----------------------------------------------------------------------
    def _get_mismatch_threshold(self, duration: float) -> float:
        """Dynamic safe-mismatch threshold based on source duration."""
        if duration < 60:
            return 2.0
        if duration < 600:
            return 4.0
        return 10.0  # ≥ 10 min

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
    # GATE-6 size check
    # -----------------------------------------------------------------------
    def _gate6_check(self, src_size: int, out_size: int,
                     label: str = "") -> Tuple[bool, str]:
        reduction = (src_size - out_size) / src_size if src_size > 0 else 0
        abs_savings_mb = (src_size - out_size) / (1024 * 1024)
        src_size_mb = src_size / (1024 * 1024)

        if src_size_mb < _MICRO_FILE_THRESHOLD_MB:
            if reduction >= _MICRO_FILE_PERCENTAGE_SAVINGS:
                return True, (f"GATE-6 PASS: {label} saves {reduction*100:.1f}% "
                              f"({abs_savings_mb:.1f}MB)")
            return False, (f"GATE-6 FAIL: {label} saves {reduction*100:.1f}% "
                           f"(need ≥{_MICRO_FILE_PERCENTAGE_SAVINGS*100:.0f}% for micro-file)")

        if reduction >= _MIN_PERCENTAGE_SAVINGS or abs_savings_mb >= _MIN_ABSOLUTE_SAVINGS_MB:
            return True, (f"GATE-6 PASS: {label} saves {reduction*100:.1f}% "
                          f"({abs_savings_mb:.1f}MB)")
        return False, (f"GATE-6 FAIL: {label} saves {reduction*100:.1f}% / "
                       f"{abs_savings_mb:.1f}MB (need ≥"
                       f"{_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR "
                       f"≥{_MIN_ABSOLUTE_SAVINGS_MB}MB)")

    # -----------------------------------------------------------------------
    # tmpfs space pre-check (NEW)
    # -----------------------------------------------------------------------
    def _check_tmpfs_space(self, src_size: int) -> bool:
        try:
            usage = shutil.disk_usage(self.prcs_dir)
            needed = src_size * 3  # rough: video encode + 2 mux candidates
            if usage.free < needed:
                console.print(
                    f"  [red]Insufficient tmpfs: {_format_size(usage.free)} free, "
                    f"need ~{_format_size(needed)}[/]"
                )
                return False
            return True
        except Exception:
            return True

    # -----------------------------------------------------------------------
    # HEVC video-only encode (tmpfs) — Using non-blocking selectors
    # -----------------------------------------------------------------------
    def _encode_hevc_video(self, src: Path, video_streams: List[Dict[str, Any]],
                           use_fallback: bool) -> Optional[Path]:
        tmp_video = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_hevc_tmp.mkv"
        if tmp_video.exists():
            tmp_video.unlink()

        cmd_args: List[str] = []
        height = int(video_streams[0].get('height', 720))
        quality = self._compute_video_quality(height, not use_fallback)

        is_interlaced = self._is_interlaced(src)
        if is_interlaced:
            console.print("  [dim]Interlaced content detected — enabling yadif[/]")

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
            base.extend(["-init_hw_device", f"vaapi=hw:{self.gpu_device}",
                         "-filter_hw_device", "hw"])

        cmd = base + ["-v", "error", "-stats", "-i", str(src)] + cmd_args + ["-an", str(tmp_video)]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            sel = selectors.DefaultSelector()
            sel.register(process.stderr, selectors.EVENT_READ)
            
            try:
                while process.poll() is None:
                    # Non-blocking check with a 100ms timeout
                    events = sel.select(timeout=0.1)
                    for key, _ in events:
                        line = key.fileobj.readline()
                        if line:
                            line = line.strip()
                            if "time=" in line:
                                parts = line.split()
                                time_part = next((p for p in parts if p.startswith("time=")), None)
                                speed_part = next((p for p in parts if p.startswith("speed=")), None)
                                speed = speed_part if speed_part else ""
                                if time_part:
                                    console.print(f"\r  [dim]Encoding: {time_part} {speed}[/]", end="")
                
                # Drain any remaining lines left in the pipe buffer after exit
                for line in process.stderr:
                    pass
            finally:
                sel.unregister(process.stderr)
                sel.close()
                if process.stderr:
                    process.stderr.close()
            
            console.print()
            
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
    # Mux single candidate — FIXED: proper a: stream specifiers
    # -----------------------------------------------------------------------
    def _mux_candidate(self, src: Path, tmp_video: Path,
                       audio_streams: List[Dict[str, Any]],
                       audio_combo: Tuple[int, ...], label: str,
                       subtitle_count: int = 0,
                       attachment_count: int = 0) -> Optional[Path]:
        out = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_{label}.mkv"
        if out.exists():
            out.unlink()

        cmd = [_FFMPEG, "-y", "-v", "error", "-i", str(tmp_video), "-i", str(src)]

        for a_idx, track in enumerate(audio_streams):
            is_opus = bool(audio_combo[a_idx]) if a_idx < len(audio_combo) else False
            if is_opus:
                src_br = track.get("bit_rate")
                try:
                    src_kbps = (int(src_br) / 1000) if src_br else 96.0
                except (ValueError, TypeError):
                    src_kbps = 96.0
                channels = int(track.get("channels", 2))
                tier = self._pick_opus_tier(src_kbps, channels)
                cmd.extend([
                    "-map", f"1:a:{a_idx}",
                    f"-c:a:{a_idx}", "libopus",
                    f"-b:a:{a_idx}", tier["br"],
                    f"-vbr:a:{a_idx}", "on",
                    f"-compression_level:a:{a_idx}", "6",
                    f"-frame_duration:a:{a_idx}", tier["frame"],
                    f"-application:a:{a_idx}", tier["app"]
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

        cmd.extend(["-map", "0:v:0", "-c:v:0", "copy",
                    "-map_metadata", "1", str(out)])

        try:
            subprocess.run(cmd, check=True)
            return out
        except subprocess.CalledProcessError:
            if out.exists():
                out.unlink()
            return None
        except OSError as e:
            console.print(f"  [bold red]RAM DISK FULL during mux: {e}[/]")
            if out.exists():
                out.unlink()
            return None

    # -----------------------------------------------------------------------
    # Generate candidates — Per-stream Opus decision
    # -----------------------------------------------------------------------
    def _mux_audio_candidates(self, src: Path, tmp_video: Path,
                              audio_streams: List[Dict[str, Any]],
                              subtitle_count: int = 0,
                              attachment_count: int = 0
                              ) -> Tuple[Optional[Path], Optional[Path]]:
        if not audio_streams:
            out = self._mux_candidate(src, tmp_video, [], (),
                                       label="cand_mute",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
            return None, out

        n = len(audio_streams)
        copy_combo = tuple([0] * n)
        out_copy = self._mux_candidate(src, tmp_video, audio_streams, copy_combo,
                                       label="cand_copy",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)

        # Per-stream Opus decision: skip re-encoding if already Opus (prevents lossy→lossy)
        opus_combo = tuple(
            0 if (s.get('codec_name') or '').lower() == 'opus' else 1
            for s in audio_streams
        )
        
        # If all streams are already Opus, skip the Opus candidate entirely
        if all(c == 0 for c in opus_combo):
            console.print("  [dim]All source audio streams already Opus — skipping Opus candidate[/]")
            return None, out_copy

        out_opus = self._mux_candidate(src, tmp_video, audio_streams, opus_combo,
                                       label="cand_opus",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
        return out_opus, out_copy

    # -----------------------------------------------------------------------
    # Black frame ratio — FIXED: fast seek + exception logging + cache
    # -----------------------------------------------------------------------
    def _black_ratio(self, path: Path) -> float:
        key = str(path)
        if key in self._black_ratio_cache:
            return self._black_ratio_cache[key]
        try:
            meta = self._probe(path)
            if not meta:
                self._black_ratio_cache[key] = 0.0
                return 0.0
            duration = float(meta.get('format', {}).get('duration', 0) or 0)

            if duration > 60:
                sample_duration = min(45, duration / 2 - 1)
                if sample_duration <= 0:
                    sample_duration = 30
                cmd = [
                    _FFMPEG, "-v", "error",
                    "-ss", "0", "-t", str(sample_duration), "-i", str(path),
                    "-ss", str(max(0, duration - sample_duration)),
                    "-t", str(sample_duration), "-i", str(path),
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
            black_duration = 0.0
            for line in result.stderr.splitlines():
                if "black_duration" in line:
                    for part in line.split():
                        if part.startswith("black_duration:"):
                            try:
                                black_duration += float(part.split(":")[1])
                            except ValueError:
                                pass
            ratio = (black_duration / total_check_duration
                      if total_check_duration > 0 else 0.0)
            self._black_ratio_cache[key] = ratio
            return ratio
        except Exception as e:
            console.print(f"  [dim]black_ratio probe failed for {path.name}: {e}[/]")
            self._black_ratio_cache[key] = 0.0
            return 0.0

    # -----------------------------------------------------------------------
    # GATE-1/2: Duration check — returns mismatch_info if fail
    # -----------------------------------------------------------------------
    def _check_gate12(self, src: Path, src_meta: Dict[str, Any],
                      output: Path) -> Optional[Dict[str, float]]:

        out_meta = self._probe(output)
        if not out_meta:
            return None

        src_fmt_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
        out_fmt_dur = float(out_meta.get('format', {}).get('duration', 0) or 0)

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']
        src_a = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
        out_a = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'audio']

        mismatch: Optional[Dict[str, float]] = None

        if src_v and out_v:
            try:
                sv_dur = float(src_v[0].get('duration') or src_fmt_dur)
                ov_dur = float(out_v[0].get('duration') or out_fmt_dur)
                if sv_dur > 0 and abs(sv_dur - ov_dur) > 0.5:
                    delta = abs(sv_dur - ov_dur)
                    mismatch = {"video_delta": delta, "audio_delta": 0.0,
                                "max_delta": 0.0}
                    console.print(f"  [bold yellow]GATE-1 FAIL:[/] "
                                  f"Video Δ={delta:.2f}s")
            except (ValueError, TypeError):
                pass

        if src_a and out_a:
            try:
                sa_dur = float(src_a[0].get('duration') or src_fmt_dur)
                oa_dur = float(out_a[0].get('duration') or out_fmt_dur)
                if sa_dur > 0 and abs(sa_dur - oa_dur) > 0.5:
                    delta = abs(sa_dur - oa_dur)
                    if mismatch is None:
                        mismatch = {"video_delta": 0.0, "audio_delta": delta,
                                    "max_delta": 0.0}
                    else:
                        mismatch["audio_delta"] = delta
                    console.print(f"  [bold yellow]GATE-2 FAIL:[/] "
                                  f"Audio Δ={delta:.2f}s")
            except (ValueError, TypeError):
                pass

        if mismatch is not None:
            mismatch["max_delta"] = max(mismatch["video_delta"],
                                         mismatch["audio_delta"])
        return mismatch

    # -----------------------------------------------------------------------
    # GATE-3/4/5: Quality checks
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
                if not ((ow == exp_w and oh == exp_h) or
                        (ow == exp_h and oh == exp_w)):
                    return False, "GATE-3", (
                        f"Dimension mismatch (src {exp_w}x{exp_h}, "
                        f"out {ow}x{oh})"), False
            except (ValueError, TypeError) as e:
                return False, "GATE-3", f"Dimension unreadable: {e}", False

        # GATE-4: Integrity
        out_sz = output.stat().st_size
        if out_sz == 0:
            return False, "GATE-4", "Output is zero bytes", False
        if not self._is_playable(output):
            return False, "GATE-4", "Output failed null-render decode", False

        # GATE-5: Black ratio
        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | "
                          f"out: {out_black*100:.1f}%[/]")
            if out_black >= 0.05 and out_black > src_black + 0.02:
                return False, "GATE-5", (
                    f"Output {out_black*100:.1f}% black vs "
                    f"source {src_black*100:.1f}%"), True

        return True, "", "GATE-3/4/5 passed", False

    # -----------------------------------------------------------------------
    # Full early-gate verification (GATE-1/2 → GATE-7 → GATE-3/4/5)
    # -----------------------------------------------------------------------
    def _verify_early_gates(self, src: Path, src_meta: Dict[str, Any],
                            output: Path
                            ) -> Tuple[bool, str, str, bool,
                                       Optional[Dict[str, Any]]]:

        mismatch_info = self._check_gate12(src, src_meta, output)

        if mismatch_info is not None:
            duration = float(src_meta.get('format', {}).get('duration', 0) or 0)
            threshold = self._get_mismatch_threshold(duration)
            max_delta = mismatch_info["max_delta"]
            mismatch_info["broken"] = max_delta > threshold
            mismatch_info["threshold"] = threshold
            status = ("[bold red]BROKEN[/]" if mismatch_info["broken"]
                      else "[green]safe[/]")
            console.print(f"  [yellow]GATE-7:[/] Δ={max_delta:.2f}s vs "
                          f"threshold={threshold:.0f}s → {status}")

        passed, gate_failed, detail, black_probable = self._check_gate345(
            src, src_meta, output)

        return passed, gate_failed, detail, black_probable, mismatch_info

    # -----------------------------------------------------------------------
    # GATE-7 full diagnostics — only called when saving a mismatch file
    # -----------------------------------------------------------------------
    def _gate7_diagnostics(self, src: Path, src_meta: Dict[str, Any],
                           candidate: Path,
                           mismatch_info: Dict[str, Any]) -> List[str]:
        max_delta = mismatch_info["max_delta"]
        vid_delta = mismatch_info["video_delta"]
        aud_delta = mismatch_info["audio_delta"]

        all_errors: List[str] = []

        console.print(Panel(
            f"[bold yellow]⚠ GATE-7 DIAGNOSTICS[/]\n"
            f"[dim]Video Δ: {vid_delta:.2f}s | Audio Δ: {aud_delta:.2f}s | "
            f"Max Δ: {max_delta:.2f}s[/]",
            border_style="yellow"
        ))

        console.print("  [bold cyan]═══ Source ═══[/]")
        src_playable, src_errors = self._diagnostic_play(src)
        src_probe_data, src_probe_errors = self._diagnostic_probe(src)
        all_errors.extend(src_errors + src_probe_errors)
        console.print(f"    Playable: {'[green]✓[/]' if src_playable else '[red]✗[/]'}  | "
                      f"Probe: {'[green]✓[/]' if src_probe_data else '[red]✗[/]'}")
        self._display_errors("Source errors", src_errors + src_probe_errors)
        if src_probe_data:
            self._display_stream_health(src_probe_data.get('streams', []))

        console.print("  [bold cyan]═══ Mismatch Candidate ═══[/]")
        cand_playable, cand_errors = self._diagnostic_play(candidate)
        cand_probe_data, cand_probe_errors = self._diagnostic_probe(candidate)
        all_errors.extend(cand_errors + cand_probe_errors)
        console.print(f"    Playable: {'[green]✓[/]' if cand_playable else '[red]✗[/]'}  | "
                      f"Probe: {'[green]✓[/]' if cand_probe_data else '[red]✗[/]'}")
        self._display_errors("Candidate errors", cand_errors + cand_probe_errors)
        if cand_probe_data:
            self._display_stream_health(cand_probe_data.get('streams', []))

        return all_errors

    # -----------------------------------------------------------------------
    # Save winner to vault (normal path)
    # -----------------------------------------------------------------------
    def _save_to_vault(self, src: Path, staged: Path, encoder_tag: str,
                       best_tag: str, src_size: int) -> None:
        final_output = self._get_vault_path(src)
        try:
            shutil.move(str(staged), str(final_output))
        except OSError:
            shutil.copyfile(str(staged), str(final_output))
            staged.unlink()

        out_size = final_output.stat().st_size
        potato = self._potato_tier(src_size, out_size)
        console.print(f"  [bold green]All gates passed.[/] "
                      f"→ {final_output.name}  Class: {potato}")

        if SOURCE_REMOVAL:
            _black_hole(src)
            console.print(f"  [bold red]Source → black hole[/]")
        else:
            archive_dest = self.archive_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}{src.suffix}"
            shutil.move(str(src), str(archive_dest))
            console.print(f"  [bold blue]Source → prcs_cmpl/[/]")

        self._log_entry(src, "success",
                        f"{encoder_tag}/{best_tag} → vault",
                        encoder=encoder_tag, audio_mode=best_tag,
                        src_size=src_size, out_size=out_size,
                        savings_pct=round((src_size - out_size) / src_size * 100, 1)
                        if src_size > 0 else 0,
                        output_path=str(final_output),
                        source_kept=not SOURCE_REMOVAL)

    # -----------------------------------------------------------------------
    # Save winner to mismatch_vault (mismatch path)
    # -----------------------------------------------------------------------
    def _save_to_mismatch(self, src: Path, staged: Path, encoder_tag: str,
                          best_tag: str, src_size: int,
                          mismatch_info: Dict[str, Any]) -> None:
        is_broken = mismatch_info.get("broken", False)
        suffix = "_mismatch_broken" if is_broken else "_mismatch"
        final_output = self._get_mismatch_path(src, suffix)

        try:
            shutil.move(str(staged), str(final_output))
        except OSError:
            shutil.copyfile(str(staged), str(final_output))
            staged.unlink()

        out_size = final_output.stat().st_size
        max_delta = mismatch_info["max_delta"]
        threshold = mismatch_info.get("threshold", 0.0)
        status_tag = "mismatch_broken" if is_broken else "mismatch_clean"

        diag_errors = self._gate7_diagnostics(src, self._probe(src) or {},
                                              final_output, mismatch_info)
        error_summary = f" | ⚠{len(diag_errors)} warnings" if diag_errors else ""

        console.print(
            f"  [bold green]GATE-7 WIN (Δ={max_delta:.2f}s vs {threshold:.0f}s):[/] "
            f"→ {final_output.name}{error_summary}\n"
            f"  [dim]{_format_size(src_size)} → {_format_size(out_size)}[/]\n"
            f"  [bold blue]Source RETAINED[/] (mismatch winner — source kept)"
        )

        self._log_entry(src, status_tag,
                        f"{encoder_tag}/{best_tag} → mismatch_vault "
                        f"(Δ={max_delta:.2f}s, threshold={threshold:.0f}s"
                        f"{', BROKEN' if is_broken else ''})",
                        encoder=encoder_tag, audio_mode=best_tag,
                        src_size=src_size, out_size=out_size,
                        savings_pct=round((src_size - out_size) / src_size * 100, 1)
                        if src_size > 0 else 0,
                        mismatch_delta=max_delta,
                        mismatch_threshold=threshold,
                        broken=is_broken,
                        output_path=str(final_output),
                        source_kept=True,
                        warning_count=len(diag_errors))

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
            console.print("[yellow]No supported media found.[/]")
            return

        gpu_status = (f"VAAPI via {self.gpu_device}"
                      if self.gpu_device else "CPU-only")
        console.print(Panel(
            f"[bold green]▶ HV_OP V2.9.10-G7[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n"
            f"[dim]Video backend: {gpu_status}[/]\n"
            f"[dim]RAM: {self.prcs_dir}[/]\n"
            f"[dim]GATE-6: ≥{_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR "
            f"≥{_MIN_ABSOLUTE_SAVINGS_MB}MB[/]\n"
            f"[dim]GATE-6 Micro (<{_MICRO_FILE_THRESHOLD_MB}MB): "
            f"≥{_MICRO_FILE_PERCENTAGE_SAVINGS*100:.0f}%[/]\n"
            f"[bold yellow]GATE-7 safe thresholds: <1min→2s | <10min→4s | "
            f"≥10min→10s[/]\n"
            f"[dim]Opus penalty: {_OPUS_OVERHEAD_PENALTY}× | "
            f"Source-already-Opus → skip Opus candidate[/]\n"
            f"[dim]Files found: {len(source_files)}[/]",
            border_style="green"
        ))

        for idx, f in enumerate(source_files):
            console.print(Panel(
                f"[bold magenta][{idx+1}/{len(source_files)}][/] "
                f"[bold white]{f.name}[/]\n[dim]{f.parent}[/]",
                border_style="magenta"
            ))

            if not f.exists():
                console.print("  [red]File removed before processing.[/]")
                self._log_entry(f, "skip_deleted", "File removed before processing")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Deleted"))
                continue

            meta = self._probe(f)
            if not meta or 'streams' not in meta:
                console.print("  [red]Unreadable container.[/]")
                self._log_entry(f, "skip_unreadable", "Unreadable container")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Unreadable"))
                continue

            src_size = f.stat().st_size
            if src_size == 0:
                console.print("  [red]Zero byte file.[/]")
                self._log_entry(f, "skip_zero", "Zero bytes")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Zero bytes"))
                continue

            if not self._is_playable(f):
                console.print("  [red]Source unplayable pre-flight.[/]")
                self._log_entry(f, "skip_unplayable", "Source unplayable")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Unplayable"))
                continue

            video_streams = [s for s in meta['streams'] if s.get('codec_type') == 'video']
            audio_streams = [s for s in meta['streams'] if s.get('codec_type') == 'audio']
            subtitle_streams = [s for s in meta['streams'] if s.get('codec_type') == 'subtitle']
            attachment_streams = [s for s in meta['streams'] if s.get('codec_type') == 'attachment']

            if not video_streams:
                console.print("  [yellow]No video stream — bypass.[/]")
                dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                shutil.move(str(f), str(self.bypass_dir / dest_name))
                self._log_entry(f, "skip_no_video", "No video stream")
                self.session_summary.append((f.name[:25], "NO-VID", "[yellow]Bypass[/]", "No video"))
                continue

            v0 = video_streams[0]
            console.print(
                f"  [dim]Codec: {v0.get('codec_name','?')} | "
                f"{v0.get('width','?')}x{v0.get('height','?')} | "
                f"Dur: {meta.get('format',{}).get('duration','?')}s | "
                f"Size: {_format_size(src_size)} | "
                f"A: {len(audio_streams)} S: {len(subtitle_streams)} "
                f"T: {len(attachment_streams)}[/]"
            )

            if v0.get('codec_name', '').lower() == 'hevc':
                console.print("  [yellow]Already HEVC — bypass.[/]")
                dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                shutil.move(str(f), str(self.bypass_dir / dest_name))
                self._log_entry(f, "skip_hevc", "Already HEVC")
                self.session_summary.append((f.name[:25], "HEVC", "[yellow]Bypass[/]", "Already HEVC"))
                continue

            if not self._check_tmpfs_space(src_size):
                self._log_entry(f, "skip_no_ram", "Insufficient tmpfs space")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "No RAM"))
                continue

            # ================================================================
            # Stage 1: Encode HEVC video
            # ================================================================
            t0 = time.time()
            if self.gpu_device:
                console.print("  [cyan]Stage 1: HEVC encode (VAAPI GPU)...[/]")
                tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=False)
                use_fallback = False
                if tmp_video is None:
                    console.print("  [yellow]GPU failed → CPU fallback...[/]")
                    tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=True)
                    use_fallback = True
            else:
                console.print("  [cyan]Stage 1: HEVC encode (CPU libx265)...[/]")
                tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=True)
                use_fallback = True

            if tmp_video is None:
                console.print("  [bold red]HEVC encode crashed. Source untouched.[/]")
                self._log_entry(f, "fail", "HEVC encode failure (both pipelines)")
                self.session_summary.append((f.name[:25], "—", "[red]Crash[/]", "HEVC encode"))
                continue

            encoder_tag = "CPU_x265" if use_fallback else "GPU_VAAPI"
            console.print(f"  [dim]Encoded in {time.time()-t0:.1f}s via {encoder_tag}[/]")

            # ================================================================
            # Stage 2: Generate dual candidates
            # ================================================================
            console.print("  [cyan]Stage 2: Generating candidates...[/]")
            cand_opus, cand_copy = self._mux_audio_candidates(
                f, tmp_video, audio_streams,
                subtitle_count=len(subtitle_streams),
                attachment_count=len(attachment_streams)
            )

            if not cand_opus and not cand_copy:
                console.print("  [bold red]Both mux candidates failed.[/]")
                if tmp_video.exists(): tmp_video.unlink()
                self._log_entry(f, "fail", "All mux candidates failed")
                self.session_summary.append((f.name[:25], f"{encoder_tag}", "[red]Crash[/]", "Mux failed"))
                continue

            # ================================================================
            # Stage 3: Gate verification per candidate
            # ================================================================
            console.print("  [cyan]Stage 3: Gate verification (GATE-1→2→7→3→4→5)...[/]")

            clean_candidates: List[Tuple[Path, str, int]] = []
            mismatch_candidates: List[Tuple[Path, str, int, Dict[str, Any]]] = []
            black_probable_cand: Optional[Tuple[Path, str, str]] = None

            for cand, tag in [(cand_opus, "all-opus"), (cand_copy, "all-copy")]:
                if cand is None or not cand.exists():
                    continue

                passed, gate_failed, detail, black_probable, mismatch_info = \
                    self._verify_early_gates(f, meta, cand)

                if black_probable:
                    black_probable_cand = (cand, tag, detail)
                    other = cand_copy if cand == cand_opus else cand_opus
                    if other and other.exists() and other != cand:
                        other.unlink()
                    break
                elif mismatch_info is not None and passed:
                    mismatch_candidates.append(
                        (cand, tag, cand.stat().st_size, mismatch_info))
                elif mismatch_info is not None and not passed:
                    console.print(f"  [bold red]{tag.upper()} {gate_failed} FAIL:[/] "
                                  f"{detail} → dropped")
                    cand.unlink()
                elif passed:
                    clean_candidates.append((cand, tag, cand.stat().st_size))
                else:
                    console.print(f"  [bold red]{tag.upper()} {gate_failed} FAIL:[/] "
                                  f"{detail} → dropped")
                    cand.unlink()

            if black_probable_cand:
                bp_cand, bp_tag, bp_detail = black_probable_cand
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {bp_detail}")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(bp_cand), str(bp_dest))
                if tmp_video.exists(): tmp_video.unlink()
                self._log_entry(f, "skip_black", f"Black probable: {bp_detail}")
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{bp_tag}",
                                            "[yellow]Black Prob[/]", bp_detail[:60]))
                continue

            if tmp_video.exists():
                tmp_video.unlink()

            if not clean_candidates and not mismatch_candidates:
                console.print("  [bold red]No candidates passed gates.[/]")
                self._log_entry(f, "fail", "No candidates passed early gates")
                self.session_summary.append((f.name[:25], encoder_tag,
                                            "[red]Crash[/]", "All gates failed"))
                continue

            # ================================================================
            # Stage 4: GATE-6 size comparison
            # ================================================================
            console.print("  [cyan]Stage 4: GATE-6 size comparison...[/]")

            # --- Case A: Only clean candidates ---
            if clean_candidates and not mismatch_candidates:
                if len(clean_candidates) == 2:
                    cand_dict = {tag: (c, s) for c, tag, s in clean_candidates}
                    opus_c, opus_s = cand_dict["all-opus"]
                    copy_c, copy_s = cand_dict["all-copy"]
                    adj_opus = int(_OPUS_OVERHEAD_PENALTY * opus_s)
                    console.print(f"  [dim]Sizes → Src: {_format_size(src_size)} | "
                                  f"Opus: {_format_size(opus_s)} | "
                                  f"Copy: {_format_size(copy_s)}[/]")
                    console.print(f"  [dim]Compare: {_OPUS_OVERHEAD_PENALTY}×Opus="
                                  f"{_format_size(adj_opus)} vs Copy="
                                  f"{_format_size(copy_s)}[/]")
                    if adj_opus <= copy_s:
                        best_cand, best_tag, best_size = opus_c, "all-opus", opus_s
                        copy_c.unlink()
                    else:
                        best_cand, best_tag, best_size = copy_c, "all-copy", copy_s
                        opus_c.unlink()
                else:
                    best_cand, best_tag, best_size = clean_candidates[0]

                adj_size = int(_OPUS_OVERHEAD_PENALTY * best_size) if best_tag == "all-opus" else best_size
                gate6_pass, gate6_detail = self._gate6_check(src_size, adj_size, best_tag)
                if not gate6_pass:
                    console.print(f"  [bold red]{gate6_detail}[/]")
                    best_cand.unlink()
                    self._log_entry(f, "fail", gate6_detail)
                    self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}",
                                                "[red]GATE-6[/]", gate6_detail[:60]))
                    continue

                console.print(f"  [green]{gate6_detail}[/]")
                self._save_to_vault(f, best_cand, encoder_tag, best_tag, src_size)
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}",
                                            "[green]Success[/]",
                                            self._potato_tier(src_size, best_size)))

            # --- Case B: Only mismatch candidates ---
            elif mismatch_candidates and not clean_candidates:
                if len(mismatch_candidates) == 2:
                    cand_dict = {tag: (c, s, mi) for c, tag, s, mi in mismatch_candidates}
                    opus_c, opus_s, opus_mi = cand_dict["all-opus"]
                    copy_c, copy_s, copy_mi = cand_dict["all-copy"]
                    adj_opus = int(_OPUS_OVERHEAD_PENALTY * opus_s)
                    if adj_opus <= copy_s:
                        best_cand, best_tag, best_size, best_mi = opus_c, "all-opus", opus_s, opus_mi
                        copy_c.unlink()
                    else:
                        best_cand, best_tag, best_size, best_mi = copy_c, "all-copy", copy_s, copy_mi
                        opus_c.unlink()
                else:
                    best_cand, best_tag, best_size, best_mi = mismatch_candidates[0]

                adj_size = int(_OPUS_OVERHEAD_PENALTY * best_size) if best_tag == "all-opus" else best_size
                gate6_pass, gate6_detail = self._gate6_check(src_size, adj_size, f"mismatch/{best_tag}")
                if not gate6_pass:
                    console.print(f"  [bold red]{gate6_detail}[/]")
                    best_cand.unlink()
                    self._log_entry(f, "fail", gate6_detail)
                    self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}",
                                                "[red]GATE-6[/]", gate6_detail[:60]))
                    continue

                console.print(f"  [green]{gate6_detail}[/]")
                self._save_to_mismatch(f, best_cand, encoder_tag, best_tag,
                                       src_size, best_mi)
                status = "mismatch_broken" if best_mi.get("broken") else "mismatch_clean"
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}",
                                            f"[yellow]{status}[/]",
                                            f"Δ={best_mi['max_delta']:.2f}s"))

            # --- Case C: Both clean AND mismatch (dual-candidate GATE-6) ---
            else:
                clean_c, clean_tag, clean_s = clean_candidates[0]
                mm_c, mm_tag, mm_s, mm_info = mismatch_candidates[0]

                adj_clean = int(_OPUS_OVERHEAD_PENALTY * clean_s) if clean_tag == "all-opus" else clean_s
                adj_mm = int(_OPUS_OVERHEAD_PENALTY * mm_s) if mm_tag == "all-opus" else mm_s

                console.print(f"  [dim]Dual mode → Src: {_format_size(src_size)} | "
                              f"Clean({clean_tag}): {_format_size(clean_s)} "
                              f"(adj {_format_size(adj_clean)}) | "
                              f"Mismatch({mm_tag}): {_format_size(mm_s)} "
                              f"(adj {_format_size(adj_mm)})[/]")

                clean_pass, clean_detail = self._gate6_check(src_size, adj_clean, f"clean/{clean_tag}")
                mm_pass, mm_detail = self._gate6_check(src_size, adj_mm, f"mismatch/{mm_tag}")

                if clean_pass and (not mm_pass or adj_clean <= adj_mm):
                    console.print(f"  [green]Clean wins: {clean_detail}[/]")
                    if mm_pass:
                        console.print(f"  [yellow]Mismatch also passed but was larger → dropped[/]")
                    mm_c.unlink()
                    self._save_to_vault(f, clean_c, encoder_tag, clean_tag, src_size)
                    self.session_summary.append((f.name[:25], f"{encoder_tag}/{clean_tag}",
                                                "[green]Success[/]",
                                                self._potato_tier(src_size, clean_s)))

                elif mm_pass:
                    console.print(f"  [yellow]Mismatch wins: {mm_detail}[/]")
                    if clean_pass:
                        console.print(f"  [yellow]Clean also passed but was larger → dropped[/]")
                    clean_c.unlink()
                    self._save_to_mismatch(f, mm_c, encoder_tag, mm_tag,
                                           src_size, mm_info)
                    status = "mismatch_broken" if mm_info.get("broken") else "mismatch_clean"
                    self.session_summary.append((f.name[:25], f"{encoder_tag}/{mm_tag}",
                                                f"[yellow]{status}[/]",
                                                f"Δ={mm_info['max_delta']:.2f}s"))

                else:
                    console.print(f"  [bold red]Neither candidate passes GATE-6:[/]")
                    console.print(f"    {clean_detail}")
                    console.print(f"    {mm_detail}")
                    clean_c.unlink()
                    mm_c.unlink()
                    self._log_entry(f, "fail",
                                   f"Both failed GATE-6: clean={clean_detail}; mm={mm_detail}")
                    self.session_summary.append((f.name[:25], encoder_tag,
                                                "[red]GATE-6[/]", "Both failed"))

        # ====================================================================
        # Session Summary
        # ====================================================================
        table = Table(
            title="HV_OP V2.9.10-G7 — Matrix",
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

        self._write_result_log()


if __name__ == "__main__":
    working_path = input("Enter target workspace directory (or hit Enter for current): ").strip()
    output_path = input("Enter output directory (or hit Enter for same as source): ").strip()
    engine = AutomatedHvOpEngine(
        working_path if working_path else ".",
        output_path
    )
    engine.run()
