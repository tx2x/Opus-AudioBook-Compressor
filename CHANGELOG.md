# Changelog

All notable changes to **Opus AudioBook Compressor** are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.1] - 2026-11-23

### Fixed
- False error `Output length (…) differs from the source (…); the source may be damaged` for MP3 files without a Xing/LAME header (typically files with a large embedded cover). For such files FFmpeg only *estimates* the duration from bitrate and file size, so the estimate was wrong while the converted file was correct.
- Verification is now two-step: the output length is always compared with the audio that FFmpeg actually encoded; the comparison with the source's declared length is a hard check only when that length is reliable, otherwise it is just noted in the log.
- The progress bar now always ends at 100 % even when source durations were estimated.

## [1.1.0] - 2026-11-22

### Added
- Sound notifications without external files: **1 beep** when processing finishes successfully, **3 beeps** when any file failed, files were not processed, or an unexpected error occurred. Cancelling with Ctrl+C is silent.
- Windows uses the built-in `winsound` beep; Linux and macOS use the terminal bell.
- Sounds are **on by default**. Toggle them with `B` in the folder menu (a short beep confirms when turning on), start with `--no-sound`, or change `SOUND_DEFAULT` in the script.
- The current sound state is shown in the folder menu and in the summary, and is remembered when starting over.

### Fixed
- Crash `Unexpected error: ''` shown in the summary step on consoles with colour output enabled (an empty style name was passed to the colouring helper).

## [1.0.0] - 2026-11-20

### Added
- Initial release.
- Voice-optimised Opus conversion through FFmpeg + libopus with 5 editable presets (96 / 64 / 48 / 32 / 24 kbps, mono, VBR).
- Folder picker with ranges (`1,3-5`), refresh, and sub-folder toggle.
- Output modes: new `_opus` folders or verified replacement of originals.
- Preservation of UTF-8 tags, cover art and chapters.
- Multi-threaded conversion (8 threads by default) with a live progress bar, speed and ETA.
- Before/after size report per folder, per sub-folder and in total.
- Readable error messages and a per-run log file in `logs/`.
- Windows 10+ console support, optional automatic FFmpeg download on Windows.

[1.1.1]: https://github.com/tx2x/Opus-AudioBook-Compressor/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/tx2x/Opus-AudioBook-Compressor/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/tx2x/Opus-AudioBook-Compressor/releases/tag/v1.0.0
