# synctex-serve

A directory server for LaTeX writing: a PDF opens in a pdf.js viewer that re-renders in place, keeping its page and
scroll position, when the file changes on disk (e.g. after `latexmk -pvc`). The viewer has its own pinch zoom, so it
reads well on a tablet. Append `?raw` to a PDF's URL for the file itself.

A single Python 3 file with no dependencies beyond the standard library; pdf.js loads from a CDN.

## Usage

```bash
synctex-serve [dir] [--port=8000] [--bind=127.0.0.1]
```

Open `http://<host>:<port>/<file>.pdf`. Bind a Tailscale IP (or `0.0.0.0` inside a tailnet-only network) to read it
from another device.

## Install

```bash
git clone git@github.com:KonstantinPakulev/synctex-serve.git
ln -s "$PWD/synctex-serve/synctex-serve" ~/.local/bin/synctex-serve
```

## Roadmap

SyncTeX forward and inverse search with Neovim and vimtex: `\lv` scrolls the viewer to the cursor, a double-tap in
the viewer jumps nvim to the source line. The server pushes events to the viewer over Server-Sent Events.
