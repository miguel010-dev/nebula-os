#!/usr/bin/env python3
"""
PSSF Anti-Malware (prototype)
Single-file Python prototype of a live file-monitoring "PSSF" agent.

Features:
- Polling-based FS watcher (no external deps)
- Heuristic risk scoring (entropy, suspicious strings, file extension, exec calls)
- Quarantine (move file, record metadata)
- Mock AI-analysis hook (interface for your IA core)
- Simple HTTP admin API (built-in http.server) to list alerts and restore files
- Basic unit tests for scoring/entropy

Warning: prototype for defensive use. Review before running on prod. Always test in safe environment.

Author: Generated for Miguel (younger-than-God-level coder) — tweak freely.
"""

import os
import sys
import time
import json
import shutil
import hashlib
import math
import uuid
import argparse
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# ------------------------- CONFIG -------------------------
WATCH_DIRS = ["./watch"]
POLL_INTERVAL = 2  # seconds
QUARANTINE_DIR = "./quarantine"
ALERTS_DIR = "./alerts"
LOG_FILE = "./pssf_agent.log"
BLOCK_THRESHOLD = 0.75
WARN_THRESHOLD = 0.35
MAX_SAMPLE_BYTES = 1024 * 64  # read at most 64KB for heuristics

# suspicious substrings and their weights
SUSPICIOUS_PATTERNS = {
    "subprocess.Popen": 0.25,
    "os.system": 0.2,
    "eval(": 0.25,
    "exec(": 0.25,
    "socket.socket": 0.2,
    "requests.post": 0.15,
    "urllib.request": 0.15,
    "wget ": 0.2,
    "curl ": 0.2,
    "base64.b64decode": 0.2,
    "open('/etc/passwd'": 0.4,
    "rm -rf": 0.4,
}

RISK_BY_EXTENSION = {
    ".exe": 0.7,
    ".dll": 0.6,
    ".so": 0.5,
    ".sh": 0.5,
    ".py": 0.25,
    ".js": 0.3,
    ".bin": 0.6,
    "": 0.1,
}

# ------------------------- UTIL -------------------------

def log(msg):
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {msg}\n"
    print(line, end="")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def safe_mkdir(path):
    os.makedirs(path, exist_ok=True)


# ------------------------- HEURISTICS -------------------------

def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    entropy = 0.0
    length = len(data)
    for c in counts.values():
        p = c / length
        entropy -= p * math.log2(p)
    return entropy


def guess_file_extension(path: str) -> str:
    _, ext = os.path.splitext(path)
    return ext.lower()


def read_sample(path: str, max_bytes=MAX_SAMPLE_BYTES) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes)
    except Exception:
        return b""


def contains_suspicious_strings(sample_bytes: bytes) -> float:
    try:
        sample_text = sample_bytes.decode("utf-8", errors="ignore").lower()
    except Exception:
        sample_text = ""
    score = 0.0
    for pat, w in SUSPICIOUS_PATTERNS.items():
        if pat.lower() in sample_text:
            score += w
    # clamp
    return min(score, 1.0)


def is_binary(sample: bytes) -> bool:
    # heuristic: many null bytes or non-text bytes
    if not sample:
        return False
    text_chars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)))
    nontext = sum(1 for b in sample if b not in text_chars)
    return (nontext / max(1, len(sample))) > 0.3


def compute_risk_for_file(path: str) -> dict:
    """Return a dict containing risk_score (0..1) and feature breakdown."""
    sample = read_sample(path)
    ext = guess_file_extension(path)

    # base score from extension
    ext_score = RISK_BY_EXTENSION.get(ext, RISK_BY_EXTENSION.get("", 0.1))

    # entropy normalized (0..1) — typical text entropy < 5, compressed/encrypted > 6.5
    entropy = shannon_entropy(sample)
    entropy_norm = max(0.0, min((entropy - 4.0) / 4.5, 1.0))  # map ~4..8.5 -> 0..1

    # suspicious strings
    sus_score = contains_suspicious_strings(sample)

    # binary vs script adjustment
    binary_flag = 1.0 if is_binary(sample) else 0.0

    # size suspiciousness (very large or 0 bytes)
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    size_score = 0.0
    if size == 0:
        size_score = 0.3
    elif size > 10 * 1024 * 1024:
        size_score = 0.2

    # combine with weights
    # weights tuned for prototype
    score = (
        0.30 * ext_score
        + 0.25 * entropy_norm
        + 0.30 * sus_score
        + 0.10 * binary_flag
        + 0.05 * size_score
    )

    score = max(0.0, min(score, 1.0))

    return {
        "path": path,
        "ext_score": ext_score,
        "entropy": entropy,
        "entropy_norm": entropy_norm,
        "sus_score": sus_score,
        "binary": bool(binary_flag),
        "size": size,
        "size_score": size_score,
        "risk_score": score,
    }


# ------------------------- QUARANTINE / ACTIONS -------------------------

def make_alert(payload: dict) -> str:
    safe_mkdir(ALERTS_DIR)
    alert_id = payload.get("alert_id") or str(uuid.uuid4())
    payload["alert_id"] = alert_id
    payload["timestamp"] = datetime.utcnow().isoformat() + "Z"
    outp = os.path.join(ALERTS_DIR, f"{alert_id}.json")
    try:
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log(f"Failed to write alert: {e}")
    return alert_id


def quarantine_file(path: str, reason: str) -> dict:
    safe_mkdir(QUARANTINE_DIR)
    if not os.path.exists(path):
        return {"error": "file-not-found"}
    try:
        base = os.path.basename(path)
        newname = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}_{base}"
        dest = os.path.join(QUARANTINE_DIR, newname)
        shutil.move(path, dest)
        os.chmod(dest, 0o600)
        payload = {
            "path": path,
            "quarantine_path": dest,
            "reason": reason,
            "action": "quarantine",
        }
        alert_id = make_alert({"payload": payload})
        log(f"Quarantined {path} -> {dest} (alert {alert_id})")
        return {"status": "ok", "alert_id": alert_id, "quarantine_path": dest}
    except Exception as e:
        log(f"Quarantine failed for {path}: {e}")
        return {"error": str(e)}


def restore_from_quarantine(quarantine_path: str, restore_to: str = None) -> dict:
    if not os.path.exists(quarantine_path):
        return {"error": "not-found"}
    try:
        restore_to = restore_to or ("./restored/" + os.path.basename(quarantine_path))
        safe_mkdir(os.path.dirname(restore_to))
        shutil.move(quarantine_path, restore_to)
        os.chmod(restore_to, 0o644)
        log(f"Restored {quarantine_path} -> {restore_to}")
        return {"status": "ok", "restored_to": restore_to}
    except Exception as e:
        return {"error": str(e)}


# ------------------------- AI INTEGRATION (MOCK) -------------------------

def integrate_ai_analysis(alert_payload: dict) -> dict:
    """
    Hook where you'd call your Nebula IA core. For now returns a mock suggestion.
    The real integration should pass: diff/lines, file content (or hash), user id, repo context.
    """
    # Example: if suspicious patterns found, AI advises blocking+explain
    risk = alert_payload.get("risk_score", 0)
    explanation = []
    if risk > 0.75:
        suggestion = "quarantine"
        explanation.append("Alta probabilidade de comportamento malicioso.")
    elif risk > 0.35:
        suggestion = "review"
        explanation.append("Possível insegurança: revisar chamadas de rede/exec.")
    else:
        suggestion = "allow"
        explanation.append("Aparenta benigno, registrar evento.")

    # Return structure the PSSF expects
    return {
        "suggestion": suggestion,
        "explanation": " ".join(explanation),
        "confidence": float(risk),
    }


# ------------------------- MONITOR LOOP -------------------------

class SimpleMonitor:
    def __init__(self, watch_dirs):
        self.watch_dirs = watch_dirs
        self.seen = {}  # path -> mtime
        safe_mkdir(QUARANTINE_DIR)
        safe_mkdir(ALERTS_DIR)

    def snapshot(self):
        current = {}
        for root in self.watch_dirs:
            for dirpath, dirs, files in os.walk(root):
                for f in files:
                    p = os.path.join(dirpath, f)
                    try:
                        m = os.path.getmtime(p)
                    except Exception:
                        m = 0
                    current[p] = m
        return current

    def poll_once(self):
        current = self.snapshot()
        # detect new or modified
        for path, m in current.items():
            if path not in self.seen or self.seen[path] < m:
                # new or changed
                self.handle_change(path)
        # detect deleted? (ignored for now)
        self.seen = current

    def handle_change(self, path):
        try:
            log(f"Change detected: {path}")
            result = compute_risk_for_file(path)
            if result["risk_score"] >= BLOCK_THRESHOLD:
                # create alert and quarantine
                payload = {**result}
                ai = integrate_ai_analysis(payload)
                payload["ai_suggestion"] = ai
                payload["recommended_action"] = ai.get("suggestion")
                make_alert(payload)
                quarantine_file(path, reason=f"risk_score {result['risk_score']}")
            elif result["risk_score"] >= WARN_THRESHOLD:
                payload = {**result}
                ai = integrate_ai_analysis(payload)
                payload["ai_suggestion"] = ai
                payload["recommended_action"] = ai.get("suggestion")
                make_alert(payload)
                log(f"Warning created for {path}, suggested: {ai.get('suggestion')}")
            else:
                # low risk -> log only
                log(f"Low risk ({result['risk_score']:.3f}) for {path}")
        except Exception as e:
            log(f"Error handling change {path}: {e}")

    def run(self, interval=POLL_INTERVAL):
        log("PSSF SimpleMonitor started.")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                log("KeyboardInterrupt, exiting monitor loop.")
                break
            except Exception as e:
                log(f"Monitor loop error: {e}")
            time.sleep(interval)


# ------------------------- SIMPLE HTTP ADMIN -------------------------

class AdminHandler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        data = json.dumps(obj, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/alerts':
            alerts = []
            for f in os.listdir(ALERTS_DIR) if os.path.exists(ALERTS_DIR) else []:
                if f.endswith('.json'):
                    try:
                        with open(os.path.join(ALERTS_DIR, f), 'r', encoding='utf-8') as fh:
                            alerts.append(json.load(fh))
                    except Exception:
                        continue
            self._send_json({'alerts': alerts})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('content-length', 0))
        body = self.rfile.read(length) if length > 0 else b''
        if parsed.path == '/restore':
            try:
                data = json.loads(body.decode('utf-8'))
                qpath = data.get('quarantine_path')
                dest = data.get('restore_to')
                res = restore_from_quarantine(qpath, restore_to=dest)
                self._send_json(res)
            except Exception as e:
                self._send_json({'error': str(e)}, code=400)
        else:
            self.send_response(404)
            self.end_headers()


def start_admin_server(port=9001):
    server = HTTPServer(('0.0.0.0', port), AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"Admin HTTP API running on port {port}")
    return server


# ------------------------- CLI / RUN -------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="PSSF Anti-Malware prototype")
    parser.add_argument('--watch', '-w', nargs='*', default=WATCH_DIRS, help='directories to watch')
    parser.add_argument('--interval', '-i', type=int, default=POLL_INTERVAL, help='poll interval seconds')
    parser.add_argument('--admin-port', '-p', type=int, default=9001, help='admin http port')
    parser.add_argument('--once', action='store_true', help='run one scan pass and exit')
    args = parser.parse_args(argv)

    monitor = SimpleMonitor(args.watch)
    server = start_admin_server(port=args.admin_port)

    if args.once:
        monitor.poll_once()
        return

    try:
        monitor.run(interval=args.interval)
    finally:
        server.shutdown()


# ------------------------- UNIT TESTS (very small) -------------------------

def _test_entropy_and_scoring():
    import tempfile
    txt = "print('hello world')\n" * 100
    with tempfile.NamedTemporaryFile('wb', delete=False) as f:
        f.write(txt.encode('utf-8'))
        tpath = f.name
    r = compute_risk_for_file(tpath)
    print('text risk:', r)
    os.unlink(tpath)

    # high-entropy binary
    with tempfile.NamedTemporaryFile('wb', delete=False) as f:
        f.write(os.urandom(4096))
        bpath = f.name
    rb = compute_risk_for_file(bpath)
    print('bin risk:', rb)
    os.unlink(bpath)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'selftest':
        _test_entropy_and_scoring()
    else:
        main()
