from flask import Flask, render_template, jsonify, request
import argparse 

app = Flask(__name__, template_folder='frontend/templates', static_folder='frontend/static')
parser = argparse.ArgumentParser(prog="serve.py", add_help=True)
parser.add_argument("--debug", action="store_true", help="Print cumulative step timings for slow UI actions")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=5001)
parser.add_argument("--no-flask-debug", action="store_true", help="Disable Flask debug mode")
args = parser.parse_args()


@app.route('/')
def index():
    return render_template('index.html')


app.run(debug=(not args.no_flask_debug), host=args.host, port=int(args.port))