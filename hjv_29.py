#!/usr/bin/env python3
"""
HV_OP - V2.9.8: tmpfs Pipeline + Dual Audio Verification + Tree-Rebuilding Vault
------------------------------------------------------------------------------------
Philosophy : Absolute automation with zero unnecessary disk I/O. Processing happens
             entirely in RAM (/dev/shm). Generates both HEVC+Opus and HEVC+Copy
             variants, runs them through early gates, and compares them at GATE-6.
             Dynamic GATE-6 thresholds handle both micro-files (20% strict) and 
             massive files (10% OR 40MB absolute).
             Size outputs dynamically switch between KB and MB for micro-files.
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
_BLACK_HOLE_DIR = Path('/mnt/storage_b/system_clean/to_be_removed')
_TMFS_DIR = Path('/dev/shm/hv_op_workspace')  # RAM disk for zero-wear processing
_USER = getpass.getuser()
_FFMPEG_DIR = Path(f'/home/{_USER}/ffmpeg/bin')
SOURCE_REMOVAL = True  # True = source sent to black hole after success | False = source moved to prcs_cmpl

# GATE-6 Dynamic Savings Rules
_MIN_ABSOLUTE_SAVINGS_MB = 40      # Minimum MB savings for standard files (>=5MB)
_MIN_PERCENTAGE_SAVINGS = 0.10     # 10% minimum savings for standard files
_MICRO_FILE_THRESHOLD_MB = 5.0     # Files under this size are considered "micro"
_MICRO_FILE_PERCENTAGE_SAVINGS = 0.20  # 20% strict savings required for micro files


def _cleanup_tmpfs_exit():
    """Cleanup tmpfs on normal exit to prevent RAM leaks between runs."""
    if _TMFS_DIR.exists():
        shutil.rmtree(_TMFS_DIR, ignore_errors=True)

def _cleanup_tmpfs_signal(signum: int, frame: Any) -> None:
    """Emergency cleanup on interrupt to prevent RAM leak."""
    console.print("\n[yellow]Interrupted. Cleaning tmpfs workspace...[/]")
    _cleanup_tmpfs_exit()
    sys.exit(1)

# Register cleanup handlers
signal.signal(signal.SIGINT, _cleanup_tmpfs_signal)
signal.signal(signal.SIGTERM, _cleanup_tmpfs_signal)
atexit.register(_cleanup_tmpfs_exit)


def _resolve_binary(name: str) -> str:
    """Custom path first, system PATH fallback."""
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
    """Dynamically format size to KB or MB string."""
    if size_bytes < 1048576:  # < 1 MB
        return f"{size_bytes // 1024}KB"
    return f"{size_bytes // 1024 // 1024}MB"


def _black_hole(path: Path) -> None:
    """Drop SOURCE file into black hole dir. Watchdog handles the rest."""
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
        # Fallback for cross-filesystem or restricted mounts
        shutil.copyfile(str(path), str(dest))
        path.unlink()


def _detect_gpu_backend() -> Optional[str]:
    """Probe for working Intel VAAPI render node."""
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

        for d in (self.vault_dir, self.archive_dir, self.bypass_dir, self.black_prob_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Wipe and recreate tmpfs workspace
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
    # Tree Rebuilding Helper
    # -----------------------------------------------------------------------
    def _get_vault_path(self, src: Path) -> Path:
        """Recreate the exact source tree structure inside universal_vault/"""
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
        """Check if file is playable, with caching to avoid redundant decodes."""
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
        """Return quality parameter based on resolution and backend."""
        if use_gpu:
            # VAAPI global_quality (ICQ mode)
            if height >= 2160: return "19"
            if height >= 1080: return "22"
            if height >= 720:  return "25"
            return "27"
        else:
            # libx265 CRF
            if height >= 2160: return "20"
            if height >= 1080: return "23"
            if height >= 720:  return "26"
            return "28"

    # -----------------------------------------------------------------------
    # Audio ladder
    # -----------------------------------------------------------------------
    def _compute_audio_ladder(self, track: Dict[str, Any]) -> int:
        channels = int(track.get("channels", 2))
        src_br = track.get("bit_rate")
        try:
            src_kbps = int(src_br) // 1000 if src_br else 0
        except (ValueError, TypeError):
            src_kbps = 0

        if src_kbps <= 0:
            return 192 if channels >= 6 else 128
        if channels >= 6:
            if src_kbps >= 448: return 320
            if src_kbps >= 320: return 256
            return 192
        else:
            if src_kbps >= 256: return 160
            if src_kbps >= 128: return 128
            if src_kbps >= 96:  return 96
            return 64

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
    # HEVC video-only encode (tmpfs) with real-time progress display
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

        # Only encode primary video stream (v:0) to avoid issues with embedded cover art
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
            # Fixed VAAPI filter chain to enforce even dimensions before hwupload
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
    # Mux single candidate (tmpfs) with subtitle/attachment support
    # -----------------------------------------------------------------------
    def _mux_candidate(self, src: Path, tmp_video: Path, audio_streams: List[Dict[str, Any]],
                       audio_combo: Tuple[int, ...], label: str,
                       subtitle_count: int = 0, attachment_count: int = 0) -> Optional[Path]:
        out = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_{label}.mkv"
        if out.exists():
            out.unlink()

        # Removed useless -stats flag
        cmd = [_FFMPEG, "-y", "-v", "error", "-i", str(tmp_video), "-i", str(src)]

        for a_idx, track in enumerate(audio_streams):
            is_opus = audio_combo[a_idx] if a_idx < len(audio_combo) else False
            if is_opus:
                br = self._compute_audio_ladder(track)
                cmd.extend([
                    "-map", f"1:a:{a_idx}",
                    f"-c:a:{a_idx}", "libopus",
                    f"-b:a:{a_idx}", f"{br}k",
                    f"-vbr:a:{a_idx}", "on"
                ])
            else:
                cmd.extend([
                    "-map", f"1:a:{a_idx}",
                    f"-c:a:{a_idx}", "copy"
                ])

        for s_idx in range(subtitle_count):
            # Fixed stream specifier syntax
            cmd.extend(["-map", f"1:s:{s_idx}", f"-c:s:{s_idx}", "copy"])

        for t_idx in range(attachment_count):
            # Fixed stream specifier syntax
            cmd.extend(["-map", f"1:t:{t_idx}", f"-c:t:{t_idx}", "copy"])

        # Only map primary video from the encoded tmp_video
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
    # Black frame ratio detection (optimized with accurate seeking)
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

                # Put -ss AFTER -i for accurate seeking (prevents missing black frames)
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
    # Early Gates Verification (Gates 1-5) - Checks candidate against SOURCE
    # -----------------------------------------------------------------------
    def _verify_early_gates(self, src: Path, src_meta: Dict[str, Any], output: Path) -> Tuple[bool, str, str, bool]:
        out_meta = self._probe(output)
        if not out_meta:
            return False, "GATE-0", "Cannot probe output metadata", False

        src_fmt_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
        out_fmt_dur = float(out_meta.get('format', {}).get('duration', 0) or 0)

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']
        src_a = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
        out_a = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'audio']

        if src_v and out_v:
            try:
                sv_dur = float(src_v[0].get('duration') or src_fmt_dur)
                ov_dur = float(out_v[0].get('duration') or out_fmt_dur)
                if sv_dur > 0 and abs(sv_dur - ov_dur) > 0.5:
                    return False, "GATE-1", f"Video duration mismatch ({abs(sv_dur-ov_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-1", f"Video duration unreadable: {e}", False

        if src_a and out_a:
            try:
                sa_dur = float(src_a[0].get('duration') or src_fmt_dur)
                oa_dur = float(out_a[0].get('duration') or out_fmt_dur)
                if sa_dur > 0 and abs(sa_dur - oa_dur) > 0.5:
                    return False, "GATE-2", f"Audio duration mismatch ({abs(sa_dur-oa_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-2", f"Audio duration unreadable: {e}", False

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

        out_sz = output.stat().st_size
        if out_sz == 0:
            return False, "GATE-4", "Output is zero bytes", False
        if str(output) not in self._playable_cache:
            if not self._is_playable(output):
                return False, "GATE-4", "Output failed null-render decode", False

        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | out: {out_black*100:.1f}%[/]")
            # Added 2% tolerance to prevent false positives
            if out_black >= 0.05 and out_black > src_black + 0.02:
                return False, "GATE-5", f"Output {out_black*100:.1f}% black vs source {src_black*100:.1f}%", True

        return True, "", "Early gates passed", False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    def run(self) -> None:
        source_files: List[Path] = []
        skip_dirs = {self.vault_dir, self.archive_dir, self.bypass_dir, self.black_prob_dir}

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
            f"[bold green]▶ HV_OP V2.9.8 — TMPFS PIPELINE + DUAL AUDIO CANDIDATES + TREE VAULT[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n"
            f"[dim]Video backend: {gpu_status}[/]\n"
            f"[dim]RAM Workspace: {_TMFS_DIR}[/]\n"
            f"[dim]GATE-6 Standard: >={_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR >={_MIN_ABSOLUTE_SAVINGS_MB}MB savings[/]\n"
            f"[dim]GATE-6 Micro (<{_MICRO_FILE_THRESHOLD_MB}MB): >={_MICRO_FILE_PERCENTAGE_SAVINGS*100:.0f}% savings[/]\n"
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

            # Fix: Handle files with no video stream
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

            console.print("  [cyan]Stage 2: Generating dual audio candidates (Opus vs Copy)...[/]")

            cand_opus, cand_copy = self._mux_audio_candidates(
                f, tmp_video, audio_streams,
                subtitle_count=len(subtitle_streams),
                attachment_count=len(attachment_streams)
            )

            if tmp_video.exists():
                tmp_video.unlink()

            if not cand_opus and not cand_copy:
                console.print("  [bold red]Both audio mux candidates failed. Source untouched.[/]")
                self.session_summary.append((f.name[:25], f"{encoder_tag}/mux", "[red]Crash[/]", "All mux failed"))
                continue

            console.print("  [cyan]Stage 3: Running early gates (1-5) against source...[/]")
            
            valid_candidates = []
            black_probable_flag = False
            bp_cand = None
            bp_detail = ""
            bp_tag = ""
            
            # Verify Opus Candidate against Source
            if cand_opus and cand_opus.exists():
                passed, gate_failed, detail, black_probable = self._verify_early_gates(f, meta, cand_opus)
                if black_probable:
                    black_probable_flag = True
                    bp_cand = cand_opus
                    bp_detail = detail
                    bp_tag = "all-opus"
                elif passed:
                    valid_candidates.append((cand_opus, "all-opus", cand_opus.stat().st_size))
                else:
                    console.print(f"  [bold red]OPUS {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_opus.unlink() # Remove the failing candidate right here

            # Verify Copy Candidate against Source (Skip if Opus already triggered black probable)
            if not black_probable_flag and cand_copy and cand_copy.exists():
                passed, gate_failed, detail, black_probable = self._verify_early_gates(f, meta, cand_copy)
                if black_probable:
                    black_probable_flag = True
                    bp_cand = cand_copy
                    bp_detail = detail
                    bp_tag = "all-copy"
                elif passed:
                    valid_candidates.append((cand_copy, "all-copy", cand_copy.stat().st_size))
                else:
                    console.print(f"  [bold red]COPY {gate_failed} FAIL:[/] {detail} -> removed from RAM")
                    cand_copy.unlink() # Remove the failing candidate right here

            if black_probable_flag:
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {bp_detail} -> moving to hevc_black_probable/")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(bp_cand), str(bp_dest))
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{bp_tag}", "[yellow]Black Prob[/]", bp_detail))
                # Cleanup the other candidate if it exists and wasn't the black prob one
                if cand_opus and cand_opus.exists() and cand_opus != bp_cand: cand_opus.unlink()
                if cand_copy and cand_copy.exists() and cand_copy != bp_cand: cand_copy.unlink()
                continue

            if not valid_candidates:
                console.print("  [bold red]No candidates passed early gates. Source untouched.[/]")
                self.session_summary.append((f.name[:25], encoder_tag, "[red]Crash[/]", "Early gates failed"))
                continue

            console.print("  [cyan]Stage 4: Running GATE-6 3-way size comparison...[/]")
            
            best_cand = None
            best_tag = ""
            best_size = 0
            
            if len(valid_candidates) == 2:
                cand_dict = {tag: (cand, sz) for cand, tag, sz in valid_candidates}
                opus_cand, opus_sz = cand_dict["all-opus"]
                copy_cand, copy_sz = cand_dict["all-copy"]
                
                # Apply the 1.1x penalty to Opus size as requested
                adjusted_opus_sz = 1.1 * opus_sz
                adj_opus_sz_int = int(adjusted_opus_sz)
                
                # Dynamic KB/MB display for sizes
                src_sz_str = _format_size(src_size)
                opus_sz_str = _format_size(opus_sz)
                copy_sz_str = _format_size(copy_sz)
                adj_opus_sz_str = _format_size(adj_opus_sz_int)
                
                console.print(f"  [dim]Sizes -> Source: {src_sz_str} | Opus: {opus_sz_str} | Copy: {copy_sz_str}[/]")
                console.print(f"  [dim]Comparison: 1.1 * Opus ({adj_opus_sz_str}) v/s Copy ({copy_sz_str})[/]")
                
                if adjusted_opus_sz <= copy_sz:
                    best_cand = opus_cand
                    best_tag = "all-opus"
                    copy_cand.unlink() # Delete loser
                    console.print(f"  [green]Opus automatically wins (1.1 * Opus <= Copy).[/]")
                else:
                    best_cand = copy_cand
                    best_tag = "all-copy"
                    opus_cand.unlink() # Delete loser
                    console.print(f"  [yellow]Copy wins (1.1 * Opus > Copy, so Opus fails comparison).[/]")
            else:
                best_cand, best_tag, _ = valid_candidates[0]
                console.print(f"  [dim]Only one candidate survived early gates: {best_tag}[/]")
                
            best_size = best_cand.stat().st_size
            
            # Final check: GATE-6 size comparison (Dynamic Threshold)
            reduction = (src_size - best_size) / src_size if src_size > 0 else 0
            abs_savings_mb = (src_size - best_size) / (1024 * 1024)
            src_size_mb = src_size / (1024 * 1024)
            
            percent_pass = False
            absolute_pass = False
            
            if src_size_mb < _MICRO_FILE_THRESHOLD_MB:
                # Micro file rule: Must save at least 20% (absolute MB rule bypassed)
                percent_pass = reduction >= _MICRO_FILE_PERCENTAGE_SAVINGS
                if not percent_pass:
                    console.print(f"  [bold red]GATE-6 FAIL:[/] Winner ({best_tag}) does not save >=20% for micro-file ({reduction*100:.1f}%).")
            else:
                # Standard rule: Must save 10% OR 40MB absolute
                percent_pass = reduction >= _MIN_PERCENTAGE_SAVINGS
                absolute_pass = abs_savings_mb >= _MIN_ABSOLUTE_SAVINGS_MB
                if not percent_pass and not absolute_pass:
                    console.print(f"  [bold red]GATE-6 FAIL:[/] Winner ({best_tag}) does not save >=10% ({reduction*100:.1f}%) nor >=40MB ({abs_savings_mb:.1f}MB).")
            
            if not percent_pass and not absolute_pass:
                if best_cand.exists():
                    best_cand.unlink()
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{best_tag}", "[red]GATE-6 Fail[/]", f"Savings: {reduction*100:.1f}%"))
                continue

            staged_output = self.prcs_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_staged.mkv"
            shutil.move(str(best_cand), str(staged_output))
            
            potato = self._potato_tier(src_size, staged_output.stat().st_size)
            recipe = f"{encoder_tag} / {best_tag}"
            
            final_output = self._get_vault_path(f)
            try:
                shutil.move(str(staged_output), str(final_output))
            except OSError:
                # Fallback for cross-filesystem moves
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

        table = Table(
            title="HV_OP V2.9.8 — tmpfs Pipeline + Dual Audio + Tree Vault Matrix",
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
