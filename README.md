

---

# Batch Video Transcoder & Vault Utility

An automated, deadlock-safe Python pipeline using `ffmpeg` and `ffprobe` to compress video collections, enforce audio efficiency, and organize outputs into a central storage vault.

## Core Purpose

The script automates media library optimization by targeting high-efficiency formats while preserving system stability:

* **Re-encodes Video to HEVC (h265):** Massively reduces file sizes while maintaining target visual quality.


* **Optimizes Audio (Opus / Copy):** Selectively converts bloated or uncompressed audio streams to libopus while leaving existing Opus streams untouched.


* **Deadlock-Free Batch Processing:** Uses non-blocking I/O polling via Python's `selectors` module to process large queues without pipe locks or process freezes.


* **Container Standardization:** Ensures all outputs are muxed into clean `.mkv` containers to prevent stream/header mismatches.



---

## Code Logic: Video & Audio Strategy

```
[ Input File ] ──► FFprobe Analysis
                       │
                       ├──► Video Stream  ──► Force HEVC (libx265 / Hardware Encoder)
                       │
                       └──► Audio Stream(s) ──► Stream Inspection
                                                   ├── Is Stream Already Opus? ──► COPY
                                                   └── Is Stream Non-Opus?     ──► OPUS

```

### 1. Video Codec Execution

* **Target:** **HEVC (H.265)**
* All input video streams are converted to HEVC. This delivers optimal compression efficiency, cutting source file sizes down significantly while retaining visual fidelity.



### 2. Audio Codec Strategy: `[ COPY / OPUS ]`

Instead of blindly re-encoding all audio, the script inspects every audio track independently:

* **`copy` (Pass-Through):** If an audio track is *already* in **Opus** format, the script copies the bitstream directly without re-encoding. This avoids lossy-to-lossy generation loss and saves processing time.


* **`opus` (Re-encode):** If an audio track uses a different codec (e.g., AAC, MP3, DTS, AC3), it is re-encoded using **`libopus`**. Opus provides superior fidelity at low bitrates compared to legacy codecs.



---

## Installation & Requirements

Ensure System Dependencies are installed and available in your `PATH`:

* **Python 3.8+**
* **FFmpeg**
* **FFprobe**

```bash
# Arch Linux / CachyOS
sudo pacman -S ffmpeg python

```

---

## Usage

Run the script directly via terminal:

```bash
python hjv_29_12.py /path/to/input_folder /path/to/output_vault

```

### Features

* **Safe Cleanups:** Session files are tracked dynamically to prevent collisions during concurrent runs.


* **Smart Extension Handling:** Standardizes vault output extensions to `.mkv` for container consistency.



---
