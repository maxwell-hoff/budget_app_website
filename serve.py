import os
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory, abort

app = Flask(__name__, template_folder='frontend/templates', static_folder='frontend/static')

DOWNLOADS_DIR = Path(__file__).resolve().parent / 'frontend' / 'static' / 'downloads'

PLATFORM_FOLDERS = {
    'mac-arm': 'mac-arm',
    'mac-x64': 'mac-x64',
    'windows': 'windows',
}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/download/<platform>')
def download(platform):
    folder_name = PLATFORM_FOLDERS.get(platform)
    if not folder_name:
        abort(404)

    folder = DOWNLOADS_DIR / folder_name
    if not folder.is_dir():
        abort(404)

    files = [f for f in folder.iterdir() if f.is_file() and not f.name.startswith('.')]
    if not files:
        abort(404)

    target = files[0]
    return send_from_directory(folder, target.name, as_attachment=True)


@app.route('/notify', methods=['POST'])
def notify():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip()
    if not email or '@' not in email:
        return jsonify({'ok': False, 'error': 'invalid email'}), 400

    # Not stored anywhere — just surfaced in the dev logs to copy manually.
    timestamp = datetime.now().isoformat(timespec='seconds')
    print(f"\n=== NOTIFY SIGNUP === {timestamp} === {email} ===\n", flush=True)

    return jsonify({'ok': True})


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(prog="serve.py", add_help=True)
    parser.add_argument("--debug", action="store_true", help="Print cumulative step timings for slow UI actions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--no-flask-debug", action="store_true", help="Disable Flask debug mode")
    args = parser.parse_args()
    app.run(debug=(not args.no_flask_debug), host=args.host, port=int(args.port))