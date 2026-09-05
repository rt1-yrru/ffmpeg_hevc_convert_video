
#!/usr/bin/env python3
"""
HV_OP - V2.9.0: tmpfs Pipeline + Exhaustive Audio Mixing + Tree-Rebuilding Vault
------------------------------------------------------------------------------------
Philosophy : Absolute automation with zero unnecessary disk I/O. Processing happens
             entirely in RAM (/dev/shm). Multi-audio files race every permutation of
             copy-vs-opus to find the absolute smallest size. Final outputs in
             universal_vault/ perfectly mirror the source directory tree.
             Black hole is strictly reserved for original source files.
"""

import os
import sys
import glob
import subprocess
import shutil
import time
import getpass
import uuid
from pathlib import Path
from itertools import product
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
_TMFS_DIR = Path('/dev/shm/hv_op_workspace') # RAM disk for zero-wear processing
_USER = getpass.getuser()
_FFMPEG_DIR = Path(f'/home/{_USER}/ffmpeg/bin')
SOURCE_REMOVAL = True  # True = source sent to black hole after success | False = source moved to prcs_cmpl

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

_FFMPEG  = _resolve_binary('ffmpeg')
_FFPROBE = _resolve_binary('ffprobe')

def _black_hole(path: Path):
    """Drop SOURCE file into black hole dir. Watchdog handles the rest."""
    if not path.exists():
        return
    _BLACK_HOLE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BLACK_HOLE_DIR / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
    shutil.move(str(path), str(dest))

def _detect_gpu_backend() -> str | None:
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
        self.input_dir  = Path(target_directory).resolve()
        self.output_dir = Path(output_directory).resolve() if output_directory.strip() else self.input_dir

        self.vault_dir        = self.output_dir / "universal_vault"
        self.archive_dir      = self.output_dir / "prcs_cmpl"
        self.bypass_dir       = self.output_dir / "bypass_file"
        self.black_prob_dir   = self.output_dir / "hevc_black_probable"

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
        self.session_summary = []

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
            rel_path = Path(src.name) # Fallback if outside root
            
        dest_dir = self.vault_dir / rel_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        clean = dest_dir / f"{rel_path.stem}_vault{rel_path.suffix}"
        if not clean.exists():
            return clean
        return dest_dir / f"{rel_path.stem}_{uuid.uuid4().hex[:8]}_vault{rel_path.suffix}"

    # -----------------------------------------------------------------------
    # Probe
    # -----------------------------------------------------------------------
    def _probe(self, path: Path) -> dict | None:
        try:
            cmd = [_FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout)
        except Exception:
            return None

    def _is_playable(self, path: Path) -> bool:
        try:
            subprocess.run([_FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"], check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    # -----------------------------------------------------------------------
    # Audio ladder
    # -----------------------------------------------------------------------
    def _compute_audio_ladder(self, track: dict) -> int:
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
        if src_size <= 0: return "Unknown"
        savings = ((src_size - tgt_size) / src_size) * 100
        if savings >= 95:  return f"Transparent (-{savings:.1f}%)"
        if savings >= 75:  return f"Optimal (-{savings:.1f}%)"
        if savings >= 40:  return f"Dense (-{savings:.1f}%)"
        if savings >= 10:  return f"Bland Potato (-{savings:.1f}%)"
        if savings > 0:    return f"Marginal (-{savings:.1f}%)"
        return f"Negative Delta (+{abs(savings):.1f}%)"

    # -----------------------------------------------------------------------
    # HEVC video-only encode (tmpfs)
    # -----------------------------------------------------------------------
    def _encode_hevc_video(self, src: Path, video_streams: list, use_fallback: bool) -> Path | None:
        tmp_video = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_hevc_tmp.mkv"
        if tmp_video.exists(): tmp_video.unlink()

        cmd_args = []
        for v_idx in range(len(video_streams)):
            if use_fallback:
                cmd_args.extend(["-map", f"0:v:{v_idx}", f"-c:v:{v_idx}", "libx265", "-crf", "24", "-preset", "slow", f"-vf:v:{v_idx}", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"])
            else:
                cmd_args.extend(["-map", f"0:v:{v_idx}", f"-c:v:{v_idx}", "hevc_vaapi", "-global_quality", "25", f"-vf:v:{v_idx}", "format=nv12,hwupload,scale_vaapi=w=trunc(iw/2)*2:h=trunc(ih/2)*2"])

        base = [_FFMPEG, "-y"] if use_fallback else [_FFMPEG, "-y", "-init_hw_device", f"vaapi=hw:{self.gpu_device}", "-filter_hw_device", "hw"]
        cmd = base + ["-v", "error", "-stats", "-i", str(src)] + cmd_args + ["-an", str(tmp_video)]

        try:
            subprocess.run(cmd, check=True)
            return tmp_video
        except subprocess.CalledProcessError:
            if tmp_video.exists(): tmp_video.unlink()
            return None
        except OSError as e:
            console.print(f"  [bold red]RAM DISK FULL during video encode: {e}[/]")
            if tmp_video.exists(): tmp_video.unlink()
            return None

    # -----------------------------------------------------------------------
    # Mux single candidate (tmpfs)
    # -----------------------------------------------------------------------
    def _mux_candidate(self, src: Path, tmp_video: Path, audio_streams: list, audio_combo: tuple, label: str) -> Path | None:
        out = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_{label}.mkv"
        if out.exists(): out.unlink()

        cmd = [_FFMPEG, "-y", "-v", "error", "-stats", "-i", str(tmp_video), "-i", str(src)]

        for a_idx, track in enumerate(audio_streams):
            is_opus = audio_combo[a_idx] if a_idx < len(audio_combo) else False
            if is_opus:
                br = self._compute_audio_ladder(track)
                cmd.extend(["-map", f"1:a:{a_idx}", f"-c:a:{a_idx}", "libopus", f"-b:a:{a_idx}", f"{br}k", f"-vbr:a:{a_idx}", "on"])
            else:
                cmd.extend(["-map", f"1:a:{a_idx}", f"-c:a:{a_idx}", "copy"])

        cmd.extend(["-map", "0:v", "-c:v", "copy", "-map_metadata", "1", str(out)])

        try:
            subprocess.run(cmd, check=True)
            return out
        except subprocess.CalledProcessError:
            if out.exists(): out.unlink()
            return None
        except OSError as e:
            console.print(f"  [bold red]RAM DISK FULL during audio mux: {e}[/]")
            if out.exists(): out.unlink()
            return None

    # -----------------------------------------------------------------------
    # Exhaustive Audio Mixing Race
    # -----------------------------------------------------------------------
    def _mux_all_audio_combinations(self, src: Path, tmp_video: Path, audio_streams: list) -> tuple[Path | None, str]:
        if not audio_streams:
            out = self._mux_candidate(src, tmp_video, [], (), label="cand_mute")
            return (out, "mute") if (out and out.exists()) else (None, "Mux failed")

        n = len(audio_streams)
        # Cap combinations to prevent RAM exhaustion on files with 10+ audio tracks
        MAX_COMBINATIONS = 16 
        if 2**n > MAX_COMBINATIONS:
            console.print(f"  [yellow]Warning: {n} audio tracks ({2**n} combos). Capping to {MAX_COMBINATIONS} to save RAM.[/]")
            combinations = list(product([0, 1], repeat=n))[:MAX_COMBINATIONS] 
        else:
            combinations = list(product([0, 1], repeat=n))

        best_path = None
        best_size = float('inf')
        best_combo = None

        # "King of the Hill" approach: keep only the smallest file in RAM to prevent OOM
        for i, combo in enumerate(combinations):
            out = self._mux_candidate(src, tmp_video, audio_streams, combo, label=f"c_{i}")
            if out and out.exists():
                sz = out.stat().st_size
                if sz < best_size:
                    if best_path and best_path.exists():
                        best_path.unlink() # Instantly free RAM from old king
                    best_path = out
                    best_size = sz
                    best_combo = combo
                else:
                    out.unlink() # Loser, instantly free RAM

        if not best_path:
            return None, "All audio combinations failed"

        tags = [f"T{idx}:{'opus' if is_opus else 'copy'}" for idx, is_opus in enumerate(best_combo)]
        decision_str = ", ".join(tags)

        return best_path, decision_str

    # -----------------------------------------------------------------------
    # Black frame ratio detection
    # -----------------------------------------------------------------------
    def _black_ratio(self, path: Path) -> float:
        try:
            cmd = [_FFMPEG, "-v", "error", "-i", str(path), "-vf", "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10", "-an", "-f", "null", "-"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            lines = result.stderr.splitlines()
            black_duration = 0.0
            for line in lines:
                if "black_duration" in line:
                    for part in line.split():
                        if part.startswith("black_duration:"):
                            try: black_duration += float(part.split(":")[1])
                            except ValueError: pass
            meta = self._probe(path)
            total = float(meta.get('format', {}).get('duration', 0) or 0) if meta else 0
            return black_duration / total if total > 0 else 0.0
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # 6-gate sequential verification
    # -----------------------------------------------------------------------
    def _verify_gates(self, src: Path, src_meta: dict, output: Path, src_size: int) -> tuple[bool, str, str, bool]:
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
            if not out_v: return False, "GATE-3", "Video stream lost in output", False
            try:
                sw, sh = int(src_v[0].get('width', 0)), int(src_v[0].get('height', 0))
                ow, oh = int(out_v[0].get('width', 0)), int(out_v[0].get('height', 0))
                exp_w, exp_h = (sw // 2) * 2, (sh // 2) * 2
                if not ((ow == exp_w and oh == exp_h) or (ow == exp_h and oh == exp_w)):
                    return False, "GATE-3", f"Dimension mismatch (src {exp_w}x{exp_h}, out {ow}x{oh})", False
            except (ValueError, TypeError) as e:
                return False, "GATE-3", f"Dimension unreadable: {e}", False

        out_sz = output.stat().st_size
        if out_sz == 0: return False, "GATE-4", "Output is zero bytes", False
        if not self._is_playable(output): return False, "GATE-4", "Output failed null-render decode", False

        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | out: {out_black*100:.1f}%[/]")
            if out_black >= 0.05 and out_black > src_black:
                return False, "GATE-5", f"Output {out_black*100:.1f}% black vs source {src_black*100:.1f}%", True

        if src_size > 0:
            reduction = (src_size - out_sz) / src_size
            if reduction < 0.10:
                return False, "GATE-6", f"Output not 10% smaller than source ({reduction*100:.1f}% reduction)", False

        return True, "", "All 6 gates passed", False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    def run(self):
        source_files = []
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
            f"[bold green]▶ HV_OP V2.9.0 — TMPFS PIPELINE + EXHAUSTIVE AUDIO MIX + TREE VAULT[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n[dim]Video backend: {gpu_status}[/]\n[dim]RAM Workspace: {_TMFS_DIR}[/]\n[dim]Files found: {len(source_files)}[/]",
            border_style="green"
        ))

        for idx, f in enumerate(source_files):
            console.print(Panel(f"[bold magenta][{idx+1}/{len(source_files)}][/] [bold white]{f.name}[/]\n[dim]{f.parent}[/]", border_style="magenta"))

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

            if video_streams:
                v0 = video_streams[0]
                console.print(f"  [dim]Codec: {v0.get('codec_name','?')} | {v0.get('width','?')}x{v0.get('height','?')} | Dur: {meta.get('format',{}).get('duration','?')}s | Size: {src_size//1024//1024}MB[/]")

            # --- HEVC bypass ---
            if video_streams and video_streams[0].get('codec_name','').lower() == 'hevc':
                console.print(f"  [yellow]Already HEVC — routing to bypass_file/[/]")
                try:
                    dest_name = f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                    shutil.move(str(f), str(self.bypass_dir / dest_name))
                    self.session_summary.append((f.name[:25], "HEVC", "[yellow]Bypassed[/]", "Moved to bypass_file/"))
                except Exception as e:
                    self.session_summary.append((f.name[:25], "HEVC", "[red]Bypass Fail[/]", str(e)[:40]))
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
            console.print(f"  [cyan]Stage 2: Racing all audio combinations ({2**len(audio_streams) if len(audio_streams) <= 4 else 'capped'} perms)...[/]")

            winner, audio_decision = self._mux_all_audio_combinations(f, tmp_video, audio_streams)

            # Done with tmp video ("a") in RAM - delete instantly to free RAM, do NOT black hole
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
                # GATE-5 black probable — move from RAM to disk (hevc_black_probable/), source untouched
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {detail} -> moving to hevc_black_probable/")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(staged_output), str(bp_dest))
                self.session_summary.append((f.name[:25], recipe, "[yellow]Black Prob[/]", detail))
                continue

            if not passed:
                # Hard gate failure — instant RAM cleanup (unlink), source untouched
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

            # Source fate based on REMOVAL flag (Black hole is STRICTLY for original source only)
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
            title="HV_OP V2.9.0 — tmpfs Pipeline + Exhaustive Audio + Tree Vault Matrix",
            box=box.DOUBLE_EDGE, expand=True
        )
        table.add_column("Asset",           ratio=3, style="cyan")
        table.add_column("Pipeline",        ratio=3, justify="center")
        table.add_column("Result",          ratio=2)
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