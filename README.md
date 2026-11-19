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

The server announces each new version of a PDF to its open viewers over Server-Sent Events, once the file has stopped
changing, so a viewer never loads a half-written file; the browser reconnects on its own after a drop (e.g. a
suspended tablet tab).

## Forward search

```bash
synctex-serve forward <line> <column> <tex> <pdf> [--port=8000] [--bind=127.0.0.1]
```

looks the source position up in the PDF's `.synctex.gz` (the `synctex` command, from TeX Live) and scrolls every
viewer of that PDF to the typeset boxes, which flash. If no server answers on the port, it starts one, detached, that
serves the PDF's directory (log in `$TMPDIR/synctex-serve-<port>.log`). The PDF must be built with `-synctex=1`
(vimtex's default).

With vimtex, `\lv` does it:

```lua
vim.g.vimtex_view_method = "general"
vim.g.vimtex_view_general_viewer = "synctex-serve"
vim.g.vimtex_view_general_options = "forward @line @col @tex @pdf --bind=0.0.0.0"
```

Inside display math (`align`, `equation`) TeX records no box per source line, so a lookup there lands on the nearest
line that has one, usually the text around the equation.

## Roadmap

Inverse search: a double-tap in the viewer jumps nvim to the source line, through nvim's RPC socket and
`vimtex#view#inverse_search`.
