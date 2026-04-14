# ansi_vt100

A terminal-based ANSI / VT100 byte-stream viewer built with Python and curses.
This is a tool that you can use to inspect an ANSI/VT100 stream stored in a file.
Developed on Linux so I'm unsure how well it will do on Windows or MacOS.

<img width="600" height="701" alt="Screenshot from 2026-04-14 08-30-17" src="https://github.com/user-attachments/assets/1ecc3af3-7ea1-4151-a40f-7e77456c7389" />

## Features

- **Split-pane TUI** — top pane shows the emulated screen, bottom pane shows a decoded event log
- **Multiple input modes** — file, hex string, live stdin pipe, or built-in demo
- **Step-through debugging** — advance one parsed token or one raw byte at a time
- **CP437 rendering** — outputs raw CP437 bytes for authentic ANSI art display
- **Color support** — SGR attributes including bold and 8-color foreground/background
- **CSI decoding** — cursor movement, erase display/line, cursor positioning, and graphic rendition
- **ASCII side view** — inspect raw hex and ASCII for the selected event

## Requirements

- Python 3.10+
- A terminal with CP437-compatible font (most Linux terminal emulators)
- No external dependencies — uses only the Python standard library
- Linux (debian): fonts-pc, fonts-pc-extra

## Installation

```bash
git clone https://github.com/bradcolbert/ansi_vt100.git
cd ansi_vt100
```

No `pip install` needed — it's a single self-contained script.

## Usage

```bash
# Built-in demo
python vt100_viewer_live.py

# View an ANSI art file
python vt100_viewer_live.py samples/4d_telephonics.ansi

# Truncate to first N lines before parsing
python vt100_viewer_live.py samples/0day_bbs.ansi --head-lines 24

# Parse hex bytes directly
python vt100_viewer_live.py --hex "48 65 6C 6C 6F 0D 0A 1B 5B 32 4A"

# Live pipe from a network source
telnet towel.blinkenlights.nl 23 | tee capture.bin | python vt100_viewer_live.py --stdin
```

## Keyboard Controls

| Key | Action                              |
|-----|-------------------------------------|
| `q` | Quit                                |
| `s` | Step one parsed token               |
| `n` | Step one raw byte                   |
| `g` | Run continuously                    |
| `p` | Pause                               |
| `r` | Reset / replay from buffered data   |
| `j` | Move selected log line down         |
| `k` | Move selected log line up           |
| `G` | Jump to end / parse all buffered data |
| `h` | Toggle help footer                  |

## Samples

The `samples/` directory contains ANSI art files for testing:

- `4d_telephonics.ansi` — graphic ANSI art with block characters and color
- `0day_bbs.ansi` — BBS-style ANSI art

## How It Works

1. **Input** is read as raw bytes (CP437-encoded for `.ansi` files)
2. **Parsing** walks the byte stream, identifying printable characters, control codes (CR, LF, BS, TAB), and ANSI escape sequences (CSI codes)
3. **Screen emulation** maintains a virtual screen buffer with cursor position, cell characters, and SGR attributes
4. **Rendering** uses curses for the UI frame and log panel, but writes screen cell content directly to the terminal as raw CP437 bytes with ANSI color escapes — this ensures authentic rendering on terminals with CP437-compatible fonts

## License

[MIT](LICENSE)
