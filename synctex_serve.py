#!/usr/bin/env python3
# synctex-serve — serve a directory over HTTP; a PDF opens in a pdf.js viewer that
# re-renders in place, keeping its page and scroll position, when the file
# changes on disk (e.g. after latexmk). Append ?raw to a PDF's URL for the file.
#   synctex-serve [dir] [--port=8000] [--bind=127.0.0.1]
# Forward search: scroll the viewers of a PDF to the box typeset from a source line, starting the server, detached,
# if none answers on the port. For vimtex's general viewer, options 'forward @line @col @tex @pdf'.
#   synctex-serve forward line column tex pdf [--port=8000] [--bind=127.0.0.1]

import argparse
import functools
import http.server
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

EVENTS_PATH = '/.synctex/events'
FORWARD_PATH = '/.synctex/forward'
INVERSE_PATH = '/.synctex/inverse'
# A PDF is announced once its size and modification time hold for one interval: latexmk writes it over the whole
# run, and a viewer that loads it midway gets a truncated file.
SETTLE_SECONDS = 0.3
KEEPALIVE_SECONDS = 15
# How soon a viewer reconnects after its stream drops, e.g. when its tab comes back from the background.
RECONNECT_MS = 500
SERVER_START_SECONDS = 5
DEFAULT_BIND = '127.0.0.1'
DEFAULT_PORT = 8000

VIEWER_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'viewer')
VIEWER_PATH = '/.synctex/'


# The open event streams, each a queue of the jumps for the PDF its viewer shows, and each PDF's latest jump: a viewer
# whose stream dropped (iOS suspends a tab in the background) missed the jumps sent meanwhile, and on reconnecting it
# gets the latest one it has not seen. A jump's id is the time it was published, so ids stay ordered across restarts.
class Subscribers:

    def __init__(self):
        self.lock = threading.Lock()
        self.pdf_paths = {}
        self.latest_jumps = {}

    # The stream's queue and the id of its start, which the stream sends first: a viewer that has received no jump yet
    # still reconnects with an id, and the jumps published while it was away are newer.
    def subscribe(self, pdf_path, seen_jump_id):
        jumps = queue.Queue()
        with self.lock:
            self.pdf_paths[jumps] = pdf_path
            latest = self.latest_jumps.get(pdf_path)
            if seen_jump_id is not None and latest is not None and latest[0] > seen_jump_id:
                jumps.put(latest)
            connect_id = time.time_ns()
        return jumps, connect_id

    def unsubscribe(self, jumps):
        with self.lock:
            del self.pdf_paths[jumps]

    def publish(self, pdf_path, jump):
        with self.lock:
            entry = (time.time_ns(), jump)
            self.latest_jumps[pdf_path] = entry
            matching = [jumps for jumps, path in self.pdf_paths.items() if path == pdf_path]
            for jumps in matching:
                jumps.put(entry)
        return len(matching)


def file_stamp(path):
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return f'{stat.st_mtime_ns}-{stat.st_size}'


# The page typeset from a source position and its boxes on that page, in PDF points from its top-left corner.
def synctex_view(line, column, tex, pdf):
    output = subprocess.run(['synctex', 'view', '-i', f'{line}:{column}:{tex}', '-o', pdf],
                            capture_output=True, text=True, timeout=10).stdout
    records = []
    for row in output.splitlines():
        key, _, value = row.partition(':')
        if key == 'Page':
            records.append({'page': int(value)})
        elif key in ('h', 'v', 'W', 'H') and records:
            records[-1][key] = float(value)
    if not records:
        return None
    page_num = records[0]['page']
    boxes = {(r['h'], r['v'] - r['H'], r['W'], r['H']) for r in records if r['page'] == page_num}
    return page_num, [{'left': left, 'top': top, 'width': width, 'height': height}
                      for left, top, width, height in sorted(boxes, key=lambda box: (box[1], box[0]))]


# The source file and line typeset at a point of a page, in PDF points from its top-left corner.
def synctex_edit(page_num, x, y, pdf):
    output = subprocess.run(['synctex', 'edit', '-o', f'{page_num}:{x}:{y}:{pdf}'],
                            capture_output=True, text=True, timeout=10).stdout
    fields = dict(row.partition(':')[::2] for row in output.splitlines())
    if 'Input' not in fields or 'Line' not in fields:
        return None
    return os.path.normpath(fields['Input']), int(fields['Line'])


# Puts the nvim listening on a socket on a source line, through vimtex, which opens the file if it is not open.
def nvim_inverse_search(nvim_socket, tex, line):
    tex_literal = "'" + tex.replace("'", "''") + "'"
    result = subprocess.run(['nvim', '--server', nvim_socket, '--remote-expr',
                             f'vimtex#view#inverse_search({line}, {tex_literal})'],
                            capture_output=True, text=True, timeout=5)
    return result.returncode == 0


class Handler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        if url.path == EVENTS_PATH:
            self.send_events(query.get('pdf', [''])[0])
        elif url.path in (VIEWER_PATH + 'viewer.js', VIEWER_PATH + 'viewer.css'):
            self.send_viewer_file(url.path.removeprefix(VIEWER_PATH))
        elif url.path.lower().endswith('.pdf') and 'raw' not in query and os.path.isfile(self.translate_path(self.path)):
            self.send_viewer_file('viewer.html')
        else:
            super().do_GET()

    def send_viewer_file(self, name):
        with open(os.path.join(VIEWER_DIR, name), 'rb') as file:
            body = file.read()
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(name))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Server-Sent Events for one PDF: 'pdf' with the file's stamp whenever it has settled on a new one, the first time
    # on connecting, and 'jump' with a forward search's page and boxes, its id the browser sends back as Last-Event-ID
    # when it reconnects. The stream ends when the viewer goes away.
    def send_events(self, pdf_path):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        file_path = self.translate_path(pdf_path)
        last_event_id = self.headers.get('Last-Event-ID')
        seen_jump_id = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
        jumps, connect_id = self.server.subscribers.subscribe(pdf_path, seen_jump_id)
        # An id with no data sets the browser's last event id without dispatching an event.
        self.wfile.write(f'retry: {RECONNECT_MS}\nid: {connect_id}\n\n'.encode())
        sent_stamp = seen_stamp = None
        last_write = time.monotonic()
        try:
            while True:
                stamp = file_stamp(file_path)
                if stamp is not None and stamp == seen_stamp and stamp != sent_stamp:
                    self.write_event('pdf', {'stamp': stamp})
                    sent_stamp = stamp
                    last_write = time.monotonic()
                seen_stamp = stamp
                try:
                    jump_id, jump = jumps.get(timeout=SETTLE_SECONDS)
                    self.write_event('jump', jump, jump_id)
                    last_write = time.monotonic()
                except queue.Empty:
                    pass
                if time.monotonic() - last_write > KEEPALIVE_SECONDS:
                    self.wfile.write(b': keepalive\n\n')
                    last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.subscribers.unsubscribe(jumps)

    def write_event(self, name, data, event_id=None):
        id_line = f'id: {event_id}\n' if event_id is not None else ''
        self.wfile.write(f'event: {name}\n{id_line}data: {json.dumps(data)}\n\n'.encode())
        self.wfile.flush()

    def do_POST(self):
        url_path = urllib.parse.urlsplit(self.path).path
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if url_path == FORWARD_PATH:
            self.forward(request)
        elif url_path == INVERSE_PATH:
            self.inverse(request)
        else:
            self.send_json(404, {'error': 'not found'})

    def forward(self, request):
        if request.get('nvim'):
            self.server.nvim_socket = request['nvim']
        pdf_rel_path = os.path.relpath(request['pdf'], self.directory)
        if pdf_rel_path.startswith('..'):
            self.send_json(400, {'error': f'{request["pdf"]} is outside the served directory {self.directory}'})
            return
        found = synctex_view(request['line'], request['column'], request['tex'], request['pdf'])
        if found is None:
            self.send_json(404, {'error': f'no box for {os.path.basename(request["tex"])}:{request["line"]} in the PDF'})
            return
        page_num, boxes = found
        pdf_path = '/' + urllib.parse.quote(pdf_rel_path)
        num_viewers = self.server.subscribers.publish(pdf_path, {'page': page_num, 'boxes': boxes})
        self.send_json(200, {'page': page_num, 'pdf_path': pdf_path, 'num_viewers': num_viewers})

    # A viewer's double tap: the source line typeset at the point, opened in the nvim that last ran a forward search.
    def inverse(self, request):
        found = synctex_edit(request['page'], request['x'], request['y'], self.translate_path(request['pdf_path']))
        if found is None:
            self.send_json(404, {'error': 'no source here'})
            return
        tex, line = found
        tex_name = f'{os.path.basename(tex)}:{line}'
        if self.server.nvim_socket is None:
            self.send_json(409, {'error': f'{tex_name}, no editor attached: press \\lv in nvim'})
            return
        if not nvim_inverse_search(self.server.nvim_socket, tex, line):
            self.send_json(409, {'error': f'{tex_name}, the editor is gone: press \\lv in nvim'})
            return
        self.send_json(200, {'tex': tex, 'line': line, 'message': f'→ {tex_name}'})

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_request(self, code='-', size='-'):
        if self.command != 'HEAD':
            super().log_request(code, size)


def serve(argv, config):
    parser = argparse.ArgumentParser(description='Serve a directory; PDFs open in a viewer that reloads on change.')
    parser.add_argument('directory', nargs='?', default='.')
    add_address_args(parser, config)
    args = parser.parse_args(argv)

    handler = functools.partial(Handler, directory=args.directory)
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as server:
        server.subscribers = Subscribers()
        server.nvim_socket = None
        print(f'Serving {os.path.abspath(args.directory)} at http://{args.bind}:{args.port}/', flush=True)
        server.serve_forever()


def forward(argv, config):
    parser = argparse.ArgumentParser(prog='synctex-serve forward',
                                     description='Scroll the viewers of a PDF to the box typeset from a source line.')
    parser.add_argument('line', type=int)
    parser.add_argument('column', type=int)
    parser.add_argument('tex')
    parser.add_argument('pdf')
    add_address_args(parser, config)
    args = parser.parse_args(argv)

    pdf = os.path.abspath(args.pdf)
    # $NVIM is the socket of the nvim that started this, e.g. through vimtex: inverse search jumps in that nvim.
    request = json.dumps({'line': args.line, 'column': args.column, 'tex': os.path.abspath(args.tex), 'pdf': pdf,
                          'nvim': os.environ.get('NVIM')})
    host = '127.0.0.1' if args.bind == '0.0.0.0' else args.bind
    url = f'http://{host}:{args.port}{FORWARD_PATH}'
    try:
        result = json.loads(post_forward(url, request, os.path.dirname(pdf), args.port, args.bind))
    except OSError as error:
        result = {'error': f'server on port {args.port} unreachable: {error}'}
    message, level = forward_message(result)
    print(message)
    nvim_notify(os.environ.get('NVIM'), message, level)


# Posts a forward search, starting the server if none answers on the port.
def post_forward(url, request, directory, port, bind):
    try:
        return post_json(url, request)
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ConnectionRefusedError):
            raise
    start_detached_server(directory, port, bind)
    deadline = time.monotonic() + SERVER_START_SECONDS
    while True:
        try:
            return post_json(url, request)
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError) or time.monotonic() > deadline:
                raise
            time.sleep(0.1)


# A forward search's outcome for the editor. No viewer connected is not a failure: a tab iOS suspended while the
# terminal was in front gets the jump when it reconnects.
def forward_message(result):
    if 'error' in result:
        return f'\\lv: {result["error"]}', 'WARN'
    if result['num_viewers'] == 0:
        return f'\\lv → page {result["page"]}, waiting for a viewer to reconnect', 'INFO'
    viewers = 'viewer' if result['num_viewers'] == 1 else 'viewers'
    return f'\\lv → page {result["page"]}, sent to {result["num_viewers"]} {viewers}', 'INFO'


# Shows a message in the nvim listening on a socket, if any, through vim.notify (the message line and :messages).
def nvim_notify(nvim_socket, message, level):
    if not nvim_socket:
        return
    message_literal = "'" + message.replace("'", "''") + "'"
    subprocess.run(['nvim', '--server', nvim_socket, '--remote-expr',
                    f"luaeval('vim.notify(_A[1], vim.log.levels[_A[2]])', [{message_literal}, '{level}'])"],
                   capture_output=True, timeout=5)


def post_json(url, body):
    request = urllib.request.Request(url, data=body.encode(), headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        return error.read().decode()


# A server in a session of its own, so it outlives the editor that started it; its output goes to a log in the temp
# directory.
def start_detached_server(directory, port, bind):
    log_path = os.path.join(tempfile.gettempdir(), f'synctex-serve-{port}.log')
    with open(log_path, 'a') as log:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), directory, f'--port={port}', f'--bind={bind}'],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def load_config():
    config_home = os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    path = os.path.join(config_home, 'synctex-serve', 'config.json')
    try:
        with open(path) as file:
            config = json.load(file)
    except FileNotFoundError:
        config = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'cannot load {path}: {error}') from error
    if not isinstance(config, dict):
        raise ValueError(f'{path}: expected a JSON object')
    unknown = set(config) - {'bind', 'port'}
    if unknown:
        raise ValueError(f'{path}: unknown keys: {", ".join(sorted(unknown))}')
    bind = config.get('bind', DEFAULT_BIND)
    port = config.get('port', DEFAULT_PORT)
    if not isinstance(bind, str) or not bind:
        raise ValueError(f'{path}: bind must be a non-empty string')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f'{path}: port must be an integer from 1 to 65535')
    return {'bind': bind, 'port': port}


def add_address_args(parser, config):
    parser.add_argument('--port', type=int, default=config['port'])
    parser.add_argument('--bind', default=config['bind'], help='address to listen on, e.g. a Tailscale IP or 0.0.0.0')


def main():
    try:
        config = load_config()
    except ValueError as error:
        sys.exit(f'synctex-serve: {error}')
    if sys.argv[1:2] == ['forward']:
        forward(sys.argv[2:], config)
    else:
        serve(sys.argv[1:], config)


if __name__ == '__main__':
    main()
