import os
import subprocess
import getpass
import json
from pathlib import Path

_dir_check='done_-1003870300217'
class IrisXeSanitizer:
    # ANSI Color Codes
    BLUE = '\033[94m'
    VIOLET = '\033[95m'
    GREEN = '\033[92m'
    RED = '\033[91m'
    ORANGE = '\033[33m'
    RESET = '\033[0m'

    def __init__(self, target_dir=_dir_check):
        self.target_dir = Path(target_dir)
        self.username = getpass.getuser()
        
        # Binary Paths
        self.__ffmpeg_base = Path(f'/home/{self.username}/ffmpeg/bin')
        self.__ffmpeg_path = self.__ffmpeg_base / 'ffmpeg'
        self.__ffprobe_path = self.__ffmpeg_base / 'ffprobe'
        
        # Intel iGPU Device
        self.va_device = '/dev/dri/renderD128'

    def get_format_info(self, file_path):
        """Uses ffprobe to get the exact container format name."""
        cmd = [
            str(self.__ffprobe_path), '-v', 'error',
            '-show_entries', 'format=format_name',
            '-of', 'json', str(file_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            raw_format = data.get('format', {}).get('format_name', '')
            
            # Take the first primary format reported (e.g., 'mov,mp4' -> 'mov')
            return raw_format.split(',')[0] if raw_format else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
            return None

    def verify_integrity(self, file_path):
        """Checks if the file is decodable using Iris Xe VA-API."""
        cmd = [
            str(self.__ffmpeg_path), '-loglevel', 'error',
            '-hwaccel', 'vaapi', '-hwaccel_device', self.va_device,
            '-i', str(file_path), '-f', 'null', '-t', '0.5', '-'
        ]
        try:
            # We check the first 0.5 seconds of the file for hardware decoding errors
            subprocess.run(cmd, capture_output=True, check=True, timeout=10)
            return True
        except subprocess.CalledProcessError:
            return False
        except Exception:
            return False

    def map_format_to_ext(self, fmt):
        """Maps FFmpeg format names to standard file extensions."""
        mapping = {
            'matroska': '.mkv',
            'mov': '.mp4',
            'mp4': '.mp4',
            'asf': '.wmv',
            'avi': '.avi',
            'mp3': '.mp3',
            'wav': '.wav',
            'flac': '.flac',
            'ogg': '.ogg',
            'png_pipe': '.png',
            'image2': '.jpg',
            'mjpeg': '.jpg',
            'jpeg_pipe': '.jpg', # Added to handle your specific question
            'pipe': '.jpg'       # Fallback for generic pipes
        }
        return mapping.get(fmt, f".{fmt}")

    def run(self, dry_run=False):
        if not self.target_dir.exists():
            print(f"{self.RED}Error: Directory '{self.target_dir}' not found.{self.RESET}")
            return

        print(f"{self.BLUE}--- Processing: {self.target_dir.absolute()} ---{self.RESET}")

        for file_path in self.target_dir.iterdir():
            if not file_path.is_file():
                continue

            print(f"{self.BLUE}Loaded: {file_path.name}{self.RESET}")

            # 1. Probe Format
            fmt = self.get_format_info(file_path)
            if not fmt:
                print(f"{self.ORANGE}Skipping: {file_path.name} (Not a recognized media format){self.RESET}")
                continue

            print(f"{self.VIOLET}Detected Format: [{fmt}]{self.RESET}")

            # 2. Verify Hardware Decode
            if not self.verify_integrity(file_path):
                print(f"{self.RED}Failure: {file_path.name} failed iGPU integrity check.{self.RESET}")
                continue

            # 3. Extension Management
            current_ext = file_path.suffix.lower()
            target_ext = self.map_format_to_ext(fmt)

            # Logic: If extension matches OR it's a MOV container inside an MP4 extension
            is_valid_mp4 = (fmt == 'mov' and current_ext == '.mp4')
            
            if current_ext == target_ext or is_valid_mp4:
                print(f"{self.GREEN}Verified: {file_path.name} is correctly named.{self.RESET}")
            else:
                new_path = file_path.with_suffix(target_ext)
                
                # Check for naming collisions
                if new_path.exists():
                    print(f"{self.RED}Error: Cannot rename, {new_path.name} already exists.{self.RESET}")
                    continue

                if not dry_run:
                    try:
                        file_path.rename(new_path)
                        print(f"{self.GREEN}Fixed: {file_path.name} -> {new_path.name}{self.RESET}")
                    except Exception as e:
                        print(f"{self.RED}Error: Rename failed: {e}{self.RESET}")
                else:
                    print(f"{self.ORANGE}[Dry Run] Would rename to: {new_path.name}{self.RESET}")

if __name__ == "__main__":
    # Change dry_run=False to actually rename files
    sanitizer = IrisXeSanitizer(_dir_check)
    sanitizer.run(dry_run=False)