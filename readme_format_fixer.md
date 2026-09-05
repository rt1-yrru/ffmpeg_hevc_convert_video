# Iris Xe Media Sanitizer & Integrity Checker

A targeted Python utility designed to probe media container formats, verify decoding integrity via Intel Iris Xe VA-API hardware acceleration, and resolve mismatched file extensions automatically.

---

## Core Purpose

Media processing pipelines often encounter mislabeled files (e.g., an MKV container named `.mp4` or a JPEG pipe misnamed `.png`) or corrupted clips that fail during video processing.

This tool serves as a **pre-flight inspector**:

* **Container Verification:** Uses `ffprobe` to determine the true underlying container or image format.


* **Hardware Decoding Check:** Tests file decodability using Intel VA-API (`/dev/dri/renderD128`) on Intel Iris Xe iGPUs.


* **Extension Normalization:** Renames mislabeled files to their correct matching extension (e.g., mapping `matroska` streams to `.mkv`).



---

## Code Logic Flow

```
[ Target File ] ──► FFprobe Inspection
                        │
                        ├──► Format Unrecognized ──► SKIP
                        │
                        └──► Valid Format Detected
                                  │
                                  ▼
                         VA-API Hardware Decode Check
                        (/dev/dri/renderD128, -t 0.5s)
                                  │
                                  ├──► Decode Fails  ──► MARK FAILED (Skip Rename)
                                  │
                                  └──► Decode Passes ──► Compare Current vs. Expected Extension
                                                                │
                                                                ├──► Matches ──► PASS
                                                                └──► Differs  ──► RENAME FILE

```

### Key Components

* **`get_format_info()`:** Executes `ffprobe` to pull JSON container details, extracting the primary format string (e.g., `mov,mp4` -> `mov`).


* **`verify_integrity()`:** Attempts a 0.5-second null decode using Intel VA-API acceleration (`-hwaccel vaapi`). If the GPU fails to decode the header/frames, the file is flagged as corrupt and bypassed.


* **`map_format_to_ext()`:** Maps detected formats (`matroska`, `mov`, `jpeg_pipe`, `png_pipe`, etc.) to clean file extensions (`.mkv`, `.mp4`, `.jpg`, `.png`).


* **Special MOV/MP4 Rule:** Treats `.mp4` extensions wrapping a `mov` container as valid without triggering unnecessary renames.



---

## Environment & Prerequisites

### 1. Hardware & Driver Requirements

* **GPU:** Intel Iris Xe / QuickSync-compatible Intel iGPU.


* **Driver Device:** Access to `/dev/dri/renderD128`.


* **VA-API Drivers:** `intel-media-driver` (LIBVA driver for Intel Gen8+ / Xe).



### 2. Binary Paths

The script expects static custom binaries located at:

* `/home/<user>/ffmpeg/bin/ffmpeg`

* `/home/<user>/ffmpeg/bin/ffprobe`


---

## Usage

1. Open the script and set your target directory in `_dir_check`:


```python
_dir_check = 'done_-1003870300217'

```


2. Run a **Dry Run** to preview changes without modifying files:
```python
sanitizer.run(dry_run=True)

```


3. Execute actual renames:


```bash
python iris_sanitizer.py

```



---
