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
# A PDF is announced once its size and modification time hold for one interval: latexmk writes it over the whole
# run, and a viewer that loads it midway gets a truncated file.
SETTLE_SECONDS = 0.3
KEEPALIVE_SECONDS = 15
SERVER_START_SECONDS = 5

VIEWER_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'viewer')
VIEWER_PATH = '/.synctex/'


# The open event streams, each a queue of the events for the PDF its viewer shows.
class Subscribers:

    def __init__(self):
        self.lock = threading.Lock()
        self.pdf_paths = {}

    def subscribe(self, pdf_path):
        events = queue.Queue()
        with self.lock:
            self.pdf_paths[events] = pdf_path
        return events

    def unsubscribe(self, events):
        with self.lock:
            del self.pdf_paths[events]

    def publish(self, pdf_path, event):
        with self.lock:
            matching = [events for events, path in self.pdf_paths.items() if path == pdf_path]
        for events in matching:
            events.put(event)
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
    # on connecting, and 'jump' with a forward search's page and boxes. The stream ends when the viewer goes away.
    def send_events(self, pdf_path):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        file_path = self.translate_path(pdf_path)
        events = self.server.subscribers.subscribe(pdf_path)
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
                    self.write_event('jump', events.get(timeout=SETTLE_SECONDS))
                    last_write = time.monotonic()
                except queue.Empty:
                    pass
                if time.monotonic() - last_write > KEEPALIVE_SECONDS:
                    self.wfile.write(b': keepalive\n\n')
                    last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.subscribers.unsubscribe(events)

    def write_event(self, name, data):
        self.wfile.write(f'event: {name}\ndata: {json.dumps(data)}\n\n'.encode())
        self.wfile.flush()

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != FORWARD_PATH:
            self.send_json(404, {'error': 'not found'})
            return
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        pdf_rel_path = os.path.relpath(request['pdf'], self.directory)
        if pdf_rel_path.startswith('..'):
            self.send_json(400, {'error': f'{request["pdf"]} is outside the served directory {self.directory}'})
            return
        found = synctex_view(request['line'], request['column'], request['tex'], request['pdf'])
        if found is None:
            self.send_json(404, {'error': f'synctex finds no box for {request["tex"]}:{request["line"]}'})
            return
        page_num, boxes = found
        pdf_path = '/' + urllib.parse.quote(pdf_rel_path)
        num_viewers = self.server.subscribers.publish(pdf_path, {'page': page_num, 'boxes': boxes})
        self.send_json(200, {'page': page_num, 'pdf_path': pdf_path, 'num_viewers': num_viewers})

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


def serve(argv):
    parser = argparse.ArgumentParser(description='Serve a directory; PDFs open in a viewer that reloads on change.')
    parser.add_argument('directory', nargs='?', default='.')
    add_address_args(parser)
    args = parser.parse_args(argv)

    handler = functools.partial(Handler, directory=args.directory)
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as server:
        server.subscribers = Subscribers()
        print(f'Serving {os.path.abspath(args.directory)} at http://{args.bind}:{args.port}/', flush=True)
        server.serve_forever()


def forward(argv):
    parser = argparse.ArgumentParser(prog='synctex-serve forward',
                                     description='Scroll the viewers of a PDF to the box typeset from a source line.')
    parser.add_argument('line', type=int)
    parser.add_argument('column', type=int)
    parser.add_argument('tex')
    parser.add_argument('pdf')
    add_address_args(parser)
    args = parser.parse_args(argv)

    pdf = os.path.abspath(args.pdf)
    request = json.dumps({'line': args.line, 'column': args.column, 'tex': os.path.abspath(args.tex), 'pdf': pdf})
    host = '127.0.0.1' if args.bind == '0.0.0.0' else args.bind
    url = f'http://{host}:{args.port}{FORWARD_PATH}'
    try:
        print(post_json(url, request))
        return
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ConnectionRefusedError):
            raise
    start_detached_server(os.path.dirname(pdf), args.port, args.bind)
    deadline = time.monotonic() + SERVER_START_SECONDS
    while True:
        try:
            print(post_json(url, request))
            return
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError) or time.monotonic() > deadline:
                raise
            time.sleep(0.1)


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


def add_address_args(parser):
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--bind', default='127.0.0.1', help='address to listen on, e.g. a Tailscale IP or 0.0.0.0')


def main():
    if sys.argv[1:2] == ['forward']:
        forward(sys.argv[2:])
    else:
        serve(sys.argv[1:])


if __name__ == '__main__':
    main()
