#!/usr/bin/env python3
"""
HV_OP - V2.7: Dual Audio Candidate Engine + 5-Gate Sequential Verification
---------------------------------------------------------------------------
Philosophy : Absolute automation. Encodes HEVC once, then races opus vs copy
             audio. Winner verified through 5 sequential gates before source fate
             is decided. Black hole dir permanently destroys unplayable outputs.
"""

import os
import sys
import subprocess
import shutil
import time
import getpass
import uuid
from pathlib import Path
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
_USER = getpass.getuser()
_FFMPEG_DIR = Path(f'/home/{_USER}/ffmpeg/bin')
SOURCE_REMOVAL = False  # True = source sent to black hole after success | False = source moved to prcs_cmpl

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
    """Drop file into black hole dir. Watchdog handles the rest."""
    _BLACK_HOLE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _BLACK_HOLE_DIR / f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"
    shutil.move(str(path), str(dest))


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
        self.prcs_dir         = self.output_dir / "prcs_file"
        self.black_prob_dir   = self.output_dir / "hevc_black_probable"

        for d in (self.vault_dir, self.archive_dir, self.bypass_dir, self.prcs_dir, self.black_prob_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.media_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.webm',
            '.flv', '.wmv', '.m4v', '.ts', '.mts', '.3gp'
        }
        self.session_summary = []

    # -----------------------------------------------------------------------
    # Probe
    # -----------------------------------------------------------------------
    def _probe(self, path: Path) -> dict | None:
        try:
            cmd = [
                _FFPROBE, "-v", "quiet", "-print_format", "json",
                "-show_streams", "-show_format", str(path)
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(res.stdout)
        except Exception:
            return None

    def _is_playable(self, path: Path) -> bool:
        try:
            subprocess.run(
                [_FFMPEG, "-v", "error", "-i", str(path), "-f", "null", "-"],
                check=True, capture_output=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    # -----------------------------------------------------------------------
    # Audio ladder
    # -----------------------------------------------------------------------
    def _compute_audio_ladder(self, track: dict) -> int:
        channels = int(track.get("channels", 2))
        src_br = track.get("bit_rate")
        if not src_br and "tags" in track:
            src_br = track["tags"].get("BPS") or track["tags"].get("BPS-eng")
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
    # HEVC video-only encode (no audio)
    # -----------------------------------------------------------------------
    def _encode_hevc_video(self, src: Path, video_streams: list, use_fallback: bool) -> Path | None:
        tmp_video = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_hevc_tmp.mkv"
        if tmp_video.exists():
            tmp_video.unlink()

        cmd_args = []
        for v_idx in range(len(video_streams)):
            if use_fallback:
                cmd_args.extend([
                    "-map", f"0:v:{v_idx}",
                    f"-c:v:{v_idx}", "libx265", "-crf", "24", "-preset", "slow",
                    f"-vf:v:{v_idx}", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
                ])
            else:
                cmd_args.extend([
                    "-map", f"0:v:{v_idx}",
                    f"-c:v:{v_idx}", "hevc_qsv", "-global_quality", "25", "-preset", "slow",
                    f"-vf:v:{v_idx}", "scale_qsv=w=trunc(iw/2)*2:h=trunc(ih/2)*2,format=nv12"
                ])

        base = ([_FFMPEG, "-y", "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
                if not use_fallback else [_FFMPEG, "-y"])
        cmd = base + ["-v", "error", "-stats", "-i", str(src)] + cmd_args + ["-an", str(tmp_video)]

        try:
            subprocess.run(cmd, check=True)
            return tmp_video
        except subprocess.CalledProcessError:
            if tmp_video.exists(): tmp_video.unlink()
            return None

    # -----------------------------------------------------------------------
    # Mux: tmp video + audio option
    # -----------------------------------------------------------------------
    def _mux_candidate(self, src: Path, tmp_video: Path,
                        audio_streams: list, use_opus: bool, label: str) -> Path | None:
        out = self.prcs_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_{label}.mkv"
        if out.exists():
            out.unlink()

        cmd = [_FFMPEG, "-y", "-v", "error", "-stats",
               "-i", str(tmp_video), "-i", str(src)]

        for a_idx, track in enumerate(audio_streams):
            if use_opus:
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

        cmd.extend(["-map", "0:v", "-c:v", "copy", "-map_metadata", "1", str(out)])

        try:
            subprocess.run(cmd, check=True)
            return out
        except subprocess.CalledProcessError:
            if out.exists(): out.unlink()
            return None

    # -----------------------------------------------------------------------
    # Pick winner between opus and copy candidates
    # opus wins only if it is at least 10% smaller than copy
    # -----------------------------------------------------------------------
    def _pick_audio_winner(self, opus_path: Path | None, copy_path: Path | None) -> tuple[Path | None, str]:
        if not opus_path and not copy_path:
            return None, "Both candidates failed"
        if not copy_path:
            return opus_path, "opus (copy failed)"
        if not opus_path:
            return copy_path, "copy (opus failed)"

        opus_sz = opus_path.stat().st_size
        copy_sz = copy_path.stat().st_size

        # opus must be at least 10% smaller than copy to win
        if opus_sz <= copy_sz * 0.90:
            return opus_path, f"opus ({opus_sz//1024}KB vs copy {copy_sz//1024}KB)"
        else:
            return copy_path, f"copy (opus not 10% smaller: {opus_sz//1024}KB vs {copy_sz//1024}KB)"

    # -----------------------------------------------------------------------
    # Black frame ratio detection
    # -----------------------------------------------------------------------
    def _black_ratio(self, path: Path) -> float:
        """Returns fraction of video duration that is black (0.0 - 1.0)."""
        try:
            cmd = [
                _FFMPEG, "-v", "error", "-i", str(path),
                "-vf", "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10",
                "-an", "-f", "null", "-"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            # blackdetect prints to stderr
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
            # get total duration via probe
            meta = self._probe(path)
            total = float(meta.get('format', {}).get('duration', 0) or 0) if meta else 0
            if total <= 0:
                return 0.0
            return black_duration / total
        except Exception:
            return 0.0

    # -----------------------------------------------------------------------
    # 6-gate sequential verification
    # Gates: 1=video dur, 2=audio dur, 3=dimensions, 4=playability,
    #        5=black detect, 6=size
    # Returns (passed, gate_failed, detail, black_probable)
    # black_probable=True is a special non-hard-fail handled by caller
    # -----------------------------------------------------------------------
    def _verify_gates(self, src: Path, src_meta: dict,
                      output: Path, src_size: int) -> tuple[bool, str, str, bool]:
        """
        Returns (passed, gate_failed, detail, black_probable).
        black_probable=True means output moves to hevc_black_probable/, source untouched.
        """
        out_meta = self._probe(output)
        if not out_meta:
            return False, "GATE-0", "Cannot probe output metadata", False

        src_fmt_dur = float(src_meta.get('format', {}).get('duration', 0) or 0)
        out_fmt_dur = float(out_meta.get('format', {}).get('duration', 0) or 0)

        src_v = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'video']
        out_v = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'video']
        src_a = [s for s in src_meta.get('streams', []) if s.get('codec_type') == 'audio']
        out_a = [s for s in out_meta.get('streams', []) if s.get('codec_type') == 'audio']

        # --- GATE 1: Video duration ---
        if src_v and out_v:
            try:
                sv_dur = float(src_v[0].get('duration') or src_fmt_dur)
                ov_dur = float(out_v[0].get('duration') or out_fmt_dur)
                if sv_dur > 0 and abs(sv_dur - ov_dur) > 0.5:
                    return False, "GATE-1", f"Video duration mismatch ({abs(sv_dur-ov_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-1", f"Video duration unreadable: {e}", False

        # --- GATE 2: Audio duration ---
        if src_a and out_a:
            try:
                sa_dur = float(src_a[0].get('duration') or src_fmt_dur)
                oa_dur = float(out_a[0].get('duration') or out_fmt_dur)
                if sa_dur > 0 and abs(sa_dur - oa_dur) > 0.5:
                    return False, "GATE-2", f"Audio duration mismatch ({abs(sa_dur-oa_dur):.2f}s delta)", False
            except (ValueError, TypeError) as e:
                return False, "GATE-2", f"Audio duration unreadable: {e}", False

        # --- GATE 3: Dimensions (handles rotation: WxH == HxW) ---
        if src_v:
            if not out_v:
                return False, "GATE-3", "Video stream lost in output", False
            try:
                sw = int(src_v[0].get('width', 0))
                sh = int(src_v[0].get('height', 0))
                ow = int(out_v[0].get('width', 0))
                oh = int(out_v[0].get('height', 0))
                exp_w = (sw // 2) * 2
                exp_h = (sh // 2) * 2
                normal_match  = (ow == exp_w and oh == exp_h)
                rotated_match = (ow == exp_h and oh == exp_w)
                if not (normal_match or rotated_match):
                    return False, "GATE-3", f"Dimension mismatch (src {exp_w}x{exp_h}, out {ow}x{oh})", False
            except (ValueError, TypeError) as e:
                return False, "GATE-3", f"Dimension unreadable: {e}", False

        # --- GATE 4: Playability ---
        out_sz = output.stat().st_size
        if out_sz == 0:
            return False, "GATE-4", "Output is zero bytes", False
        if not self._is_playable(output):
            return False, "GATE-4", "Output failed null-render decode", False

        # --- GATE 5: Black frame detection ---
        if src_v:
            src_black = self._black_ratio(src)
            out_black = self._black_ratio(output)
            console.print(f"  [dim]Black ratio — src: {src_black*100:.1f}% | out: {out_black*100:.1f}%[/]")
            if out_black >= 0.05 and out_black > src_black:
                detail = f"Output {out_black*100:.1f}% black vs source {src_black*100:.1f}%"
                return False, "GATE-5", detail, True  # black_probable — special handling

        # --- GATE 6: Size — output must be at least 10% smaller than source ---
        if src_size > 0:
            reduction = (src_size - out_sz) / src_size
            if reduction < 0.10:
                pct = reduction * 100
                return False, "GATE-6", f"Output not 10% smaller than source ({pct:.1f}% reduction)", False

        return True, "", "All 6 gates passed", False

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------
    def run(self):
        # Recursive scan
        source_files = []
        skip_dirs = {self.vault_dir, self.archive_dir, self.bypass_dir, self.prcs_dir, self.black_prob_dir}
        for root, dirs, files in os.walk(self.input_dir):
            root_path = Path(root)
            # prune internal dirs from walk
            dirs[:] = [d for d in dirs if root_path / d not in skip_dirs]
            for fname in files:
                fp = root_path / fname
                if fp.suffix.lower() in self.media_extensions:
                    source_files.append(fp)

        if not source_files:
            console.print("[yellow]No supported media found in workspace tree.[/]")
            return

        console.print(Panel(
            f"[bold green]▶ HV_OP V2.7 — DUAL AUDIO RACE + 5-GATE SEQUENTIAL VERIFICATION[/]\n"
            f"[dim]ffmpeg: {_FFMPEG}[/]\n[dim]Files found: {len(source_files)}[/]",
            border_style="green"
        ))

        for idx, f in enumerate(source_files):
            console.print(Panel(
                f"[bold magenta][{idx+1}/{len(source_files)}][/] [bold white]{f.name}[/]"
                f"\n[dim]{f.parent}[/]",
                border_style="magenta"
            ))

            # --- Pre-flight probe ---
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

            # Pre-flight playability
            if not self._is_playable(f):
                console.print(f"  [red]Source unplayable pre-flight. Skipping.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Skip[/]", "Source unplayable"))
                continue

            video_streams = [s for s in meta['streams'] if s.get('codec_type') == 'video']
            audio_streams = [s for s in meta['streams'] if s.get('codec_type') == 'audio']

            # Log pre-flight info
            if video_streams:
                v0 = video_streams[0]
                console.print(
                    f"  [dim]Codec: {v0.get('codec_name','?')} | "
                    f"{v0.get('width','?')}x{v0.get('height','?')} | "
                    f"Dur: {meta.get('format',{}).get('duration','?')}s | "
                    f"Size: {src_size//1024//1024}MB[/]"
                )

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

            # --- Stage 1: Encode HEVC video-only (QSV → CPU fallback) ---
            console.print("  [cyan]Stage 1: Encoding HEVC video (QSV)...[/]")
            t0 = time.time()
            tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=False)
            use_fallback = False

            if tmp_video is None:
                console.print("  [yellow]QSV failed. Falling back to libx265...[/]")
                tmp_video = self._encode_hevc_video(f, video_streams, use_fallback=True)
                use_fallback = True

            if tmp_video is None:
                console.print("  [bold red]HEVC encode crashed on both pipelines. Source untouched.[/]")
                self.session_summary.append((f.name[:25], "—", "[red]Crash[/]", "HEVC encode failure"))
                continue

            encoder_tag = "CPU_x265" if use_fallback else "QSV_265"
            console.print(f"  [dim]Video encoded in {time.time()-t0:.1f}s via {encoder_tag}[/]")

            # --- Stage 2: Mux candidates ---
            audio_tag = f"{len(audio_streams)}aud" if audio_streams else "mute"
            console.print("  [cyan]Stage 2: Muxing candidates (opus vs copy)...[/]")

            if audio_streams:
                opus_path = self._mux_candidate(f, tmp_video, audio_streams, use_opus=True,  label="cand_opus")
                copy_path = self._mux_candidate(f, tmp_video, audio_streams, use_opus=False, label="cand_copy")
            else:
                # No audio — just wrap video
                opus_path = self._mux_candidate(f, tmp_video, [], use_opus=True, label="cand_mute")
                copy_path = None

            # Done with tmp video
            tmp_video.unlink(missing_ok=True)

            winner, audio_decision = self._pick_audio_winner(opus_path, copy_path)

            # Clean up loser
            for cand in (opus_path, copy_path):
                if cand and cand != winner and cand.exists():
                    cand.unlink()

            if winner is None:
                console.print("  [bold red]Both mux candidates failed. Source untouched.[/]")
                self.session_summary.append((f.name[:25], f"{encoder_tag}/{audio_tag}", "[red]Crash[/]", "Mux failure"))
                continue

            console.print(f"  [dim]Audio winner: {audio_decision}[/]")

            # Winner stays in prcs_file — gates verified here before vault promotion
            staged_output = self.prcs_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_staged.mkv"
            shutil.move(str(winner), str(staged_output))

            recipe = f"{encoder_tag} / {audio_decision[:20]}"

            # --- Stage 3: 6-gate sequential verification (on staged file in prcs_file/) ---
            console.print("  [cyan]Stage 3: Running 6-gate verification in prcs_file/...[/]")
            passed, gate_failed, detail, black_probable = self._verify_gates(f, meta, staged_output, src_size)

            if black_probable:
                # GATE-5 black probable — output to hevc_black_probable/, source untouched
                console.print(f"  [bold yellow]GATE-5 BLACK PROBABLE:[/] {detail} → moving to hevc_black_probable/")
                bp_dest = self.black_prob_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_blackprob.mkv"
                shutil.move(str(staged_output), str(bp_dest))
                self.session_summary.append((f.name[:25], recipe, "[yellow]Black Probable[/]", detail))
                continue

            if not passed:
                if gate_failed == "GATE-4":
                    console.print(f"  [bold red]{gate_failed} FAIL:[/] {detail} → sending to black hole")
                    _black_hole(staged_output)
                    self.session_summary.append((f.name[:25], recipe, f"[red]{gate_failed} Fail[/]", f"Black holed: {detail}"))
                else:
                    console.print(f"  [bold red]{gate_failed} FAIL:[/] {detail} → purging staged output, source safe")
                    if staged_output.exists():
                        staged_output.unlink()
                    self.session_summary.append((f.name[:25], recipe, f"[red]{gate_failed} Fail[/]", detail))
                continue

            # All 5 gates passed — promote from prcs_file/ to universal_vault/
            out_sz = staged_output.stat().st_size
            potato = self._potato_tier(src_size, out_sz)
            final_output = self.vault_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}_vault.mkv"
            shutil.move(str(staged_output), str(final_output))
            console.print(f"  [bold green]All gates passed.[/] Promoted to universal_vault/. Class: {potato}")

            # Source fate based on REMOVAL flag
            if SOURCE_REMOVAL:
                _black_hole(f)
                console.print(f"  [bold red]Source sent to black hole[/] (SOURCE_REMOVAL=True)")
            else:
                archive_dest = self.archive_dir / f"{f.stem}_{uuid.uuid4().hex[:8]}{f.suffix}"
                shutil.move(str(f), str(archive_dest))
                console.print(f"  [bold blue]Source moved to prcs_cmpl/[/] (SOURCE_REMOVAL=False)")

            self.session_summary.append((f.name[:25], recipe, "[green]Success[/]", potato))

        table = Table(
            title="HV_OP V2.7 — Dual Audio Race + 5-Gate Verification Matrix",
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
