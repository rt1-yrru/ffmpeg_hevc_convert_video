#!/usr/bin/env python3
"""
HV_OP - V2.9.1: tmpfs Pipeline + Exhaustive Audio Mixing + Tree-Rebuilding Vault
------------------------------------------------------------------------------------
Philosophy : Absolute automation with zero unnecessary disk I/O. Processing happens
             entirely in RAM (/dev/shm). Multi-audio files race every permutation of
             copy-vs-opus to find the absolute smallest size. Final outputs in
             universal_vault/ perfectly mirror the source directory tree.
             Black hole is strictly reserved for original source files.

Changes from V2.9.0:
             - Fixed Python 3.8+ compatibility (type hints)
             - Smart audio combination capping (prioritizes all-copy, single-track opus)
             - Fixed VAAPI scale filter syntax
             - Added subtitle/attachment stream preservation
             - Cached playability checks to avoid redundant decodes
             - Optimized black ratio detection (samples first/last 45s instead of full file)
             - Added interlaced video detection with yadif deinterlace
             - Resolution-adaptive CRF/quality settings
             - FFmpeg progress display during encoding
             - Signal handlers for tmpfs cleanup on Ctrl+C
             - GATE-6: Pass if >=10% OR >=20MB absolute savings
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
from pathlib import Path
from itertools import product
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
_MIN_ABSOLUTE_SAVINGS_MB = 20  # Minimum MB savings to pass GATE-6 regardless of percentage
_MIN_PERCENTAGE_SAVINGS = 0.10  # 10% minimum savings threshold


def _cleanup_tmpfs(signum: int, frame: Any) -> None:
    """Emergency cleanup on interrupt to prevent RAM leak."""
    console.print("\n[yellow]Interrupted. Cleaning tmpfs workspace...[/]")
    if _TMFS_DIR.exists():
        try:
            shutil.rmtree(_TMFS_DIR)
            console.print("[green]tmpfs cleaned successfully.[/]")
        except Exception as e:
            console.print(f"[red]Failed to clean tmpfs: {e}[/]")
    sys.exit(1)


# Register signal handlers for graceful cleanup
signal.signal(signal.SIGINT, _cleanup_tmpfs)
signal.signal(signal.SIGTERM, _cleanup_tmpfs)


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


def _black_hole(path: Path) -> None:
    """Drop SOURCE file into black hole dir. Watchdog handles the rest."""
    if not path.exists():
        return
    _BLACK_HOLE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BLACK_HOLE_DIR / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
    shutil.move(str(path), str(dest))


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

        # Wipe and recreate tmpfs workspace to prevent stale RAM usage
        if _TMFS_DIR.exists():
            shutil.rmtree(_TMFS_DIR)
        self.prcs_dir = _TMFS_DIR / "prcs_file"
        self.prcs_dir.mkdir(parents=True, exist_ok=True)

        self.media_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.webm',
            '.flv', '.wmv', '.m4v', '.ts', '.mts', '.3gp'
        }
        self.session_summary: List[Tuple[str, str, str, str]] = []
        self._playable_cache: Dict[str, bool] = {}  # Cache playability results

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
            rel_path = Path(src.name)  # Fallback if outside root

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
        """Detect if video stream is interlaced."""
        try:
            cmd = [_FFPROBE, "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=field_order", "-of", "csv=p=0", str(path)]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            field_order = result.stdout.strip().lower()
            # Interlaced: tt, tb, bt, bb | Progressive: progressive
            return field_order in ('tt', 'tb', 'bt', 'bb')
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Resolution-adaptive Quality Settings
    # -----------------------------------------------------------------------
    def _compute_video_quality(self, height: int, use_gpu: bool) -> str:
        """Return quality parameter based on resolution."""
        if use_gpu:
            # VAAPI global_quality (lower = better, range ~1-50)
            if height >= 2160: return "20"
            if height >= 1080: return "23"
            if height >= 720:  return "26"
            return "28"
        else:
            # x265 CRF (lower = better)
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
    # HEVC video-only encode (tmpfs) with progress display
    # -----------------------------------------------------------------------
    def _encode_hevc_video(self, src: Path, video_streams: List[Dict[str, Any]],
                           use_fallback: bool) -> Optional[Path]:
        tmp_video = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_hevc_tmp.mkv"
        if tmp_video.exists():
            tmp_video.unlink()

        cmd_args = []

        # Get height for quality scaling
        height = int(video_streams[0].get('height', 720))
        quality = self._compute_video_quality(height, not use_fallback)

        # Check for interlaced content
        is_interlaced = self._is_interlaced(src)
        if is_interlaced:
            console.print("  [dim]Interlaced content detected — enabling yadif deinterlace[/]")

        for v_idx in range(len(video_streams)):
            if use_fallback:
                # CPU libx265 with proper even-dimension scaling
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
                # GPU VAAPI — fixed syntax (no trunc() which isn't supported)
                filter_chain = "format=nv12,hwupload,scale_vaapi=w=iw:h=ih"
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
            # Show progress during encoding
            process = subprocess.Popen(cmd, stderr=subprocess.PIPE, universal_newlines=True)
            for line in process.stderr:
                if "time=" in line:
                    time_part = [p for p in line.split() if p.startswith("time=")]
                    speed_part = [p for p in line.split() if p.startswith("speed=")]
                    speed = speed_part[0] if speed_part else ""
                    if time_part:
                        console.print(f"\r  [dim]Encoding: {time_part[0]} {speed}[/]", end="")
            process.wait()
            console.print()  # New line after progress

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

        cmd = [_FFMPEG, "-y", "-v", "error", "-stats", "-i", str(tmp_video), "-i", str(src)]

        # Audio streams
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

        # Subtitle streams (copy as-is)
        for s_idx in range(subtitle_count):
            cmd.extend([
                "-map", f"1:s:{s_idx}",
                "-c:s:s:{0}".format(s_idx), "copy"
            ])

        # Attachment streams (fonts, cover art, etc.)
        for t_idx in range(attachment_count):
            cmd.extend([
                "-map", f"1:t:{t_idx}",
                "-c:t:t:{0}".format(t_idx), "copy"
            ])

        cmd.extend(["-map", "0:v", "-c:v", "copy", "-map_metadata", "1", str(out)])

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
    # Exhaustive Audio Mixing Race (with smart capping)
    # -----------------------------------------------------------------------
    def _mux_all_audio_combinations(self, src: Path, tmp_video: Path,
                                     audio_streams: List[Dict[str, Any]],
                                     subtitle_count: int = 0,
                                     attachment_count: int = 0) -> Tuple[Optional[Path], str]:
        if not audio_streams:
            out = self._mux_candidate(src, tmp_video, [], (),
                                       label="cand_mute",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
            return (out, "mute") if (out and out.exists()) else (None, "Mux failed")

        n = len(audio_streams)
        MAX_COMBINATIONS = 16

        if 2 ** n > MAX_COMBINATIONS:
            console.print(f"  [yellow]Warning: {n} audio tracks ({2**n} combos). "
                         f"Smart-capping to {MAX_COMBINATIONS} to save RAM.[/]")

            # Smart prioritization instead of random first-N
            smart_combos: List[Tuple[int, ...]] = []

            # 1. All copy (baseline - always test this)
            smart_combos.append(tuple([0] * n))

            # 2. Single track opus (find best single-track optimization)
            for i in range(n):
                combo = [0] * n
                combo[i] = 1
                smart_combos.append(tuple(combo))

            # 3. Fill remaining with other useful combinations
            for combo in product([0, 1], repeat=n):
                if combo not in smart_combos:
                    smart_combos.append(combo)
                if len(smart_combos) >= MAX_COMBINATIONS:
                    break

            combinations = smart_combos[:MAX_COMBINATIONS]
        else:
            combinations = list(product([0, 1], repeat=n))

        best_path: Optional[Path] = None
        best_size = float('inf')
        best_combo: Optional[Tuple[int, ...]] = None

        # "King of the Hill" approach: keep only the smallest file in RAM
        for i, combo in enumerate(combinations):
            out = self._mux_candidate(src, tmp_video, audio_streams, combo,
                                       label=f"c_{i}",
                                       subtitle_count=subtitle_count,
                                       attachment_count=attachment_count)
            if out and out.exists():
                sz = out.stat().st_size
                if sz < best_size:
                    if best_path and best_path.exists():
                        best_path.unlink()  # Instantly free RAM from old king
                    best_path = out
                    best_size = sz
                    best_combo = combo
                else:
                    out.unlink()  # Loser, instantly free RAM

        if not best_path:
            return None, "All audio combinations failed"

        tags = [f"T{idx}:{'opus' if is_opus else 'copy'}" for idx, is_opus in enumerate(best_combo)]
        decision_str = ", ".join(tags)

        return best_path, decision_str

    # -----------------------------------------------------------------------
    # Black frame ratio detection (optimized with sampling)
    # -----------------------------------------------------------------------
    def _black_ratio(self, path: Path) -> float:
        try:
            meta = self._probe(path)
            if not meta:
                return 0.0

            duration = float(meta.get('format', {}).get('duration', 0) or 0)

            # For short files (< 60s), check everything
            # For longer files, sample first 45s and last 45s to save time
            if duration > 60:
                sample_duration = min(45, duration / 2 - 1)
                if sample_duration <= 0:
                    sample_duration = 30

                cmd = [
                    _FFMPEG, "-v", "error",
                    "-ss", "0", "-t", str(sample_duration), "-i", str(path),
                    "-ss", str(duration - sample_duration), "-t", str(sample_duration), "-i", str(path),
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
    # 6-gate sequential verification
    # -----------------------------------------------------------------------
    def _verify_gates(self, src: Path, src_meta: Dict[str, Any], output: Path,
                      src_size: int) -> Tuple[bool, str, str, bool]:
        out_meta = self._probe(output)
        if not out_meta:
            return False, "GATE-0", "Cannot probe output metadata", False

        src_fmt_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
        out_fmt_dur = float(out_meta.get('format', {}).get('duration', 0) or 0)

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']
        src_a = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
        out_a = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'audio']

        # GATE-1: Video duration
        if src_v and out_v:
            try:
                sv_dur = float(src_v[0].get('duration') or src_fmt_dur)
                ov_dur = float(out_v[0].get('duration') or out_fmt_dur)
                if sv_dur > 0 and abs(sv_dur - ov_dur) > 0.5:
                    return False, "GATE-1", f"Video duration mismatch ({abs(sv_dur-ov_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-1", f"Video duration unreadable: {e}", False

        # GATE-2: Audio duration
        if src_a and out_a:
            try:
                sa_dur = float(src_a[0].get('duration') or src_fmt_dur)
                oa_dur = float(out_a[0].get('duration') or out_fmt_dur)
                if sa_dur > 0 and abs(sa_dur - oa_dur) > 0.5:
                    return False, "GATE-2", f"Audio duration mismatch ({abs(sa_dur-oa_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-2", f"Audio duration unreadable: {e}", False

        # GATE-3: Dimensions
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

        # GATE-4: Output validity (use cache to skip if already verified)
        out_sz = output.stat().st_size
        if out_sz == 0:
            return False, "GATE-4", "Output is zero bytes", False
        if str(output) not in self._playable_cache:
            if not self._is_playable(output):
                return False, "GATE-4", "Output failed null-render decode", False

        # GATE-5: Black frame detection
        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | out: {out_black*100:.1f}%[/]")
            if out_black >= 0.05 and out_black > src_black:
                return False, "GATE-5", f"Output {out_black*100:.1f}% black vs source {src_black*100:.1f}%", True

        # GATE-6: Size reduction check
        # Pass if EITHER condition is met:
        #   A) Percentage reduction >= 10% (good for small files)
        #   B) Absolute reduction >= 20MB (good for large files where % is low but MB saved is significant)
        #
        # Example: 2GB -> 1870MB = 8.7% but saves 178MB → PASSES (condition B)
        # Example: 100MB -> 95MB = 5% and saves 5MB → FAILS (neither condition met)
        if src_size > 0:
            reduction = (src_size - out_sz) / src_size
            abs_savings_mb = (src_size - out_sz) / (1024 * 1024)

            percent_pass = reduction >= _MIN_PERCENTAGE_SAVINGS
            absolute_pass = abs_savings_mb >= _MIN_ABSOLUTE_SAVINGS_MB

            if not percent_pass and not absolute_pass:
                return False, "GATE-6", \
                    f"Output not 10% smaller ({reduction*100:.1f}%) nor 20MB smaller ({abs_savings_mb:.1f}MB saved)", False

        return True, "", "All 6 gates passed", False

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
            f"[bold green]▶ HV_OP V2.9.1 — TMPFS PIPELINE + EXHAUSTIVE AUDIO MIX + TREE VAULT[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n"
            f"[dim]Video backend: {gpu_status}[/]\n"
            f"[dim]RAM Workspace: {_TMFS_DIR}[/]\n"
            f"[dim]GATE-6: >={_MIN_PERCENTAGE_SAVINGS*100:.0f}% OR >={_MIN_ABSOLUTE_SAVINGS_MB}MB savings[/]\n"
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

            if video_streams:
                v0 = video_streams[0]
                console.print(
                    f"  [dim]Codec: {v0.get('codec_name','?')} | "
                    f"{v0.get('width','?')}x{v0.get('height','?')} | "
                    f"Dur: {meta.get('format',{}).get('duration','?')}s | "
                    f"Size: {src_size//1024//1024}MB | "
                    f"Audio: {len(audio_streams)} | Subs: {len(subtitle_streams)} | "
                    f"Attach: {len(attachment_streams)}[/]"
                )

            # --- HEVC bypass ---
            if video_streams and video_streams[0].get('codec_name', '').lower() == 'hevc':
                console.print(f"  [yellow]Already HEVC — routing to bypass_file/[/]")
                try:
                    dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                    shutil.move(str(f), str(self.bypass_dir / dest_name))
                    self.session_summary.append((f.name[:25], "HEVC", "[yellow]Bypassed[/]", "Moved to bypass_file/"))
                except Exception as e:
                    self.session_summary.append((f.name[:25], "HEVC", "[red]Bypass Fail[/]", str(e)[:80]))
                continue

            # --- Stage 1: Encode HEVC video-only (GPU VAAPI -> CPU fallback) in tmpfs ---
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

            # --- Stage 2: Exhaustive Audio Mixing Race in tmpfs ---
            audio_tag = f"{len(audio_streams)}aud" if audio_streams else "mute"
            combo_count = min(2 ** len(audio_streams), 16) if audio_streams else 1
            console.print(f"  [cyan]Stage 2: Racing audio combinations ({combo_count} perms)...[/]")

            winner, audio_decision = self._mux_all_audio_combinations(
                f, tmp_video, audio_streams,
                subtitle_count=len(subtitle_streams),
                attachment_count=len(attachment_streams)
            )

            # Done with tmp video in RAM - delete instantly to free RAM
            if tmp_video.exists():
                tmp_video.unlink()

            if winner is None:
                console.print("  [bold red]All audio mux combinations failed. Source untouched.[/]")
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{audio_tag}", "[red]Crash[/]", "All mux failed"))
                continue

            console.print(f"  [dim]Audio winner: {audio_decision}[/]")

            # Winner stays in prcs_file (RAM) for gate verification
            staged_output = self.prcs_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_staged.mkv"
            shutil.move(str(winner), str(staged_output))

            recipe = f"{encoder_tag} / {audio_decision[:25]}"

            # --- Stage 3: 6-gate sequential verification (operating purely in RAM) ---
            console.print("  [cyan]Stage 3: Running 6-gate verification...[/]")
            passed, gate_failed, detail, black_probable = self._verify_gates(f, meta, staged_output, src_size)

            if black_probable:
                # GATE-5 black probable — move from RAM to disk, source untouched
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {detail} -> moving to hevc_black_probable/")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(staged_output), str(bp_dest))
                self.session_summary.append((f.name[:25], recipe, "[yellow]Black Prob[/]", detail))
                continue

            if not passed:
                # Hard gate failure — instant RAM cleanup, source untouched
                console.print(f"  [bold red]{gate_failed} FAIL:[/] {detail} -> purged from RAM")
                if staged_output.exists():
                    staged_output.unlink()
                self.session_summary.append((f.name[:25], recipe, f"[red]{gate_failed} Fail[/]", detail))
                continue

            # --- All 6 gates passed ---
            out_sz = staged_output.stat().st_size
            potato = self._potato_tier(src_size, out_sz)

            # Move from tmpfs to final disk destination, REBUILDING the source tree structure
            final_output = self._get_vault_path(f)
            shutil.move(str(staged_output), str(final_output))
            console.print(f"  [bold green]All gates passed.[/] Promoted to vault tree. Class: {potato}")

            # Source fate based on REMOVAL flag
            if SOURCE_REMOVAL:
                _black_hole(f)
                console.print(f"  [bold red]Source sent to black hole[/] (SOURCE_REMOVAL=True)")
            else:
                archive_dest = self.archive_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                shutil.move(str(f), str(archive_dest))
                console.print(f"  [bold blue]Source moved to prcs_cmpl/[/] (SOURCE_REMOVAL=False)")

            self.session_summary.append((f.name[:25], recipe, "[green]Success[/]", potato))

        # -----------------------------------------------------------------------
        # Session Summary Table
        # -----------------------------------------------------------------------
        table = Table(
            title="HV_OP V2.9.1 — tmpfs Pipeline + Exhaustive Audio + Tree Vault Matrix",
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
