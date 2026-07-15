#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from owndifflib.common import OwnDiffError, read_json
from owndifflib.mcq import evaluate_answers, write_answers_from_mapping

TERMINAL_APP_BY_TERM_PROGRAM = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "WarpTerminal": "Warp",
    "vscode": "Visual Studio Code",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "Tabby": "Tabby",
}


class ReviewState:
    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.args = args
        self.token = token
        self.nonce = _new_token()
        self.mcq = read_json(Path(args.mcq))
        self.questions = [question for question in self.mcq.get("questions", []) if isinstance(question, dict)]
        self.done = threading.Event()
        self.exit_code = 2
        self.result: dict[str, Any] | None = None
        self.error: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Answer OwnDiff multiple choice questions in a local browser review UI.")
    parser.add_argument(
        "--mcq",
        default=".owndiff/ownership-mcq.json",
        metavar="QUESTIONS_JSON",
        help="Public multiple choice question JSON path.",
    )
    parser.add_argument("--answer-key", default=".owndiff/ownership-answer-key.json", help="Local answer key JSON path.")
    parser.add_argument("--answers-out", default=".owndiff/ownership-answers.json", help="Answers JSON output path.")
    parser.add_argument("--gate-out", default=".owndiff/ownership-gate.json", help="Gate JSON output path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate answers immediately after writing them.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1.")
    parser.add_argument("--port", type=int, default=0, help="Port to bind. Default: random free port.")
    parser.add_argument("--timeout-seconds", type=int, default=900, help="Stop waiting after this many seconds.")
    parser.add_argument("--no-open-browser", action="store_true", help="Print the URL without opening a browser.")
    parser.add_argument(
        "--no-return-to-terminal",
        action="store_true",
        help="Do not try to refocus the originating terminal app after browser submission.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.host not in {"127.0.0.1", "localhost"}:
            raise OwnDiffError("Browser review only binds to localhost for safety")
        if int(args.timeout_seconds) < 1:
            raise OwnDiffError("--timeout-seconds must be >= 1")

        token = _new_token()
        state = ReviewState(args, token)
        if not state.questions:
            print(json.dumps({"status": "not_required", "questions": 0}, sort_keys=True))
            return 0

        handler = _handler_for(state)
        server = ThreadingHTTPServer((args.host, args.port), handler)
        server.timeout = 0.25
        url = f"http://{server.server_address[0]}:{server.server_address[1]}/?token={urllib.parse.quote(token)}"
        print(f"OwnDiff browser review: {url}", file=sys.stderr, flush=True)
        if not args.no_open_browser:
            opened = _open_browser(url)
            if not opened:
                print("OwnDiff could not open the default browser automatically; use the localhost URL above.", file=sys.stderr, flush=True)

        deadline = time.monotonic() + int(args.timeout_seconds)
        while not state.done.is_set() and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()

        if not state.done.is_set():
            print("error: browser review timed out before answers were submitted", file=sys.stderr)
            return 2
        if state.error:
            print(f"error: {state.error}", file=sys.stderr)
            return 2

        print(json.dumps(state.result or {}, sort_keys=True))
        return state.exit_code
    except (OwnDiffError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("canceled: no OwnDiff answers were written", file=sys.stderr)
        return 130


def _handler_for(state: ReviewState) -> type[BaseHTTPRequestHandler]:
    class OwnDiffReviewHandler(BaseHTTPRequestHandler):
        server_version = "OwnDiffReview/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            if not _token_ok(self.path, state.token):
                self._send(403, _page("OwnDiff", "<p>Invalid or missing review token.</p>"))
                return
            self._send(200, _review_page(state, error=None), state.nonce)

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/submit":
                self._send(404, _page("OwnDiff", "<p>Not found.</p>"))
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 64_000:
                self._send(413, _page("OwnDiff", "<p>Submission too large.</p>"))
                return
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            form = urllib.parse.parse_qs(body, keep_blank_values=True)
            if form.get("token", [""])[0] != state.token:
                self._send(403, _page("OwnDiff", "<p>Invalid or missing review token.</p>"))
                return

            answers = {
                str(question.get("id")): form.get(str(question.get("id")), [""])[0].strip().lower()
                for question in state.questions
            }
            missing = [question_id for question_id, option_id in answers.items() if not option_id]
            if missing:
                self._send(
                    400,
                    _review_page(state, error=f"Answer every question before submitting. Missing: {', '.join(missing)}."),
                    state.nonce,
                )
                return

            try:
                answers_payload = write_answers_from_mapping(answers, state.args.answers_out, state.args.mcq)
                gate = (
                    evaluate_answers(
                        state.args.mcq,
                        state.args.answer_key,
                        state.args.answers_out,
                        state.args.gate_out,
                        state.args.config,
                    )
                    if state.args.evaluate
                    else None
                )
            except (OwnDiffError, ValueError) as exc:
                state.error = str(exc)
                self._send(400, _page("OwnDiff Review Error", f"<p>{html.escape(str(exc))}</p>"))
                state.done.set()
                return

            state.result = {"answers_out": str(Path(state.args.answers_out)), "answers": answers_payload["answers"]}
            if gate is not None:
                state.result.update(
                    {
                        "gate_out": str(Path(state.args.gate_out)),
                        "status": gate["status"],
                        "score_percent": gate["score_percent"],
                        "attempts": gate["attempts"],
                        "attempt_summary": gate["attempt_summary"],
                        "agent_may_push_merge_request": gate["agent_may_push_merge_request"],
                    }
                )
                state.exit_code = 0 if gate["agent_may_push_merge_request"] else 3
            else:
                state.exit_code = 0

            self._send(200, _result_page(state.result, _should_return_to_terminal(state.args), state.nonce), state.nonce)
            _schedule_return_to_terminal(state.args)
            state.done.set()

        def _send(self, status: int, body: str, nonce: str | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            script_src = f"script-src 'nonce-{nonce}'" if nonce else "script-src 'none'"
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src 'unsafe-inline'; {script_src}; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(payload)

    return OwnDiffReviewHandler


def _new_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")


def _token_ok(path: str, token: str) -> bool:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    return query.get("token", [""])[0] == token


def _review_page(state: ReviewState, error: str | None) -> str:
    risk = str(state.mcq.get("risk_level", "unknown")).lower()
    risk_label = html.escape(risk.upper())
    total = len(state.questions)
    questions_html = []
    for index, question in enumerate(state.questions, start=1):
        question_id = str(question.get("id"))
        dimension = html.escape(str(question.get("dimension") or "ownership").replace("_", " "))
        options = []
        for option in question.get("options", []):
            option_id = html.escape(str(option.get("id", "")).lower())
            option_text = html.escape(str(option.get("text", "")))
            options.append(
                "<label class='option'>"
                f"<input type='radio' name='{html.escape(question_id)}' value='{option_id}' required>"
                "<span class='option-mark' aria-hidden='true'></span>"
                f"<span class='option-id'>{option_id}</span>"
                f"<span class='option-text'>{option_text}</span>"
                "</label>"
            )
        questions_html.append(
            f"<section class='question' id='{html.escape(question_id)}'>"
            "<div class='question-top'>"
            f"<span class='question-index'>Question {index} of {total}</span>"
            f"<span class='dimension-pill'>{dimension}</span>"
            "</div>"
            f"<h2>{html.escape(str(question.get('question', '')))}</h2>"
            f"{_hint_html(question)}"
            f"<div class='options'>{''.join(options)}</div>"
            "</section>"
        )

    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    progress_items = "".join(
        f"<a href='#{html.escape(str(question.get('id')))}' data-progress-item='{html.escape(str(question.get('id')))}'>"
        f"<span>{index}</span><strong>Pending</strong></a>"
        for index, question in enumerate(state.questions, start=1)
    )
    body = (
        "<header class='topbar'><div><strong>Own Your Diff</strong><span>Local browser review</span></div>"
        f"<span class='risk risk-{html.escape(risk)}'>{risk_label}</span></header>"
        "<section class='hero'>"
        "<div class='hero-copy'>"
        "<p class='eyebrow'>Ownership Gate</p>"
        "<h1>Prove you understand this diff.</h1>"
        "<p>Answer the validated ownership checks. The agent stays blocked until every answer is correct.</p>"
        "</div>"
        "<div class='metrics'>"
        f"<div><span>Questions</span><strong>{total}</strong></div>"
        f"<div><span>Risk</span><strong>{risk_label}</strong></div>"
        "<div><span>Mode</span><strong>Browser</strong></div>"
        "</div>"
        "<div class='trust-row'>"
        "<span>Localhost only</span>"
        "<span>Answer key stays server-side</span>"
        "<span>Returns to terminal after submit</span>"
        "</div>"
        "</section>"
        f"{error_html}"
        "<form method='post' action='/submit' id='review-form'>"
        f"<input type='hidden' name='token' value='{html.escape(state.token)}'>"
        "<div class='review-layout'>"
        "<aside class='progress-panel'>"
        "<div class='panel-title'>Gate progress</div>"
        "<div class='progress-track'><span id='progress-bar'></span></div>"
        f"<div class='progress-count'><strong id='answered-count'>0</strong><span>/ {total} answered</span></div>"
        f"<nav>{progress_items}</nav>"
        "<div class='side-note'>"
        "<strong>Before you submit</strong>"
        "<span>Choose the answer that best explains the changed behavior, risk, and verification signal.</span>"
        "</div>"
        "<label class='hint-toggle'><input type='checkbox' id='hint-toggle' checked>"
        "<span>Show hints</span></label>"
        "</aside>"
        f"<div class='question-stack'>{''.join(questions_html)}</div>"
        "</div>"
        "<div class='submit-dock'><div><strong id='dock-count'>0 answered</strong><span>Gate remains locked until every question is answered.</span></div>"
        "<div class='dock-actions'><button type='button' class='secondary-button' id='retry-button'>Retry quiz</button>"
        "<button type='submit' id='submit-button' disabled>Submit gate</button></div></div>"
        "</form>"
        f"{_review_script(state.nonce, total)}"
    )
    return _page("OwnDiff Browser Review", body)


def _hint_html(question: dict[str, Any]) -> str:
    hint = str(question.get("hint") or "").strip()
    if not hint:
        return ""
    return (
        "<details class='hint-box' open data-hint>"
        "<summary>Hint</summary>"
        f"<p>{html.escape(hint)}</p>"
        "</details>"
    )


def _result_page(result: dict[str, Any], returning_to_terminal: bool, nonce: str) -> str:
    allowed = bool(result.get("agent_may_push_merge_request"))
    status = "passed" if allowed else "blocked"
    status_label = "Gate passed" if allowed else "Gate blocked"
    summary = html.escape(str(result.get("attempt_summary", "Answers submitted.")))
    note = (
        "Returning to your terminal session. The local gate artifact has been updated."
        if returning_to_terminal
        else "Return to your terminal session. The local gate artifact has been updated."
    )
    body = (
        "<header class='topbar'><div><strong>Own Your Diff</strong><span>Local browser review</span></div>"
        f"<span class='risk result-{html.escape(status)}'>{html.escape(status_label)}</span></header>"
        f"<section class='hero result'><p class='eyebrow'>OwnDiff Gate</p><h1>{html.escape(status_label)}</h1>"
        f"<p>{summary}</p>"
        "<div class='metrics'>"
        f"<div><span>Status</span><strong>{html.escape(str(result.get('status', status)))}</strong></div>"
        f"<div><span>Score</span><strong>{html.escape(str(result.get('score_percent', 0)))}%</strong></div>"
        f"<div><span>Attempts</span><strong>{html.escape(str(result.get('attempts', 0)))}</strong></div>"
        "</div>"
        "<div class='trust-row'>"
        "<span>Gate artifact updated</span>"
        "<span>Agent can continue from the terminal</span>"
        "</div></section>"
        f"<p class='note' id='close-status'>{html.escape(note)} This browser review will close automatically if your browser allows it.</p>"
        "<p class='note'><button type='button' class='secondary-button close-button' id='close-browser-button'>Close browser review</button></p>"
        f"{_close_script(nonce)}"
    )
    return _page("OwnDiff Gate Result", body)


def _schedule_return_to_terminal(args: argparse.Namespace) -> None:
    if not _should_return_to_terminal(args):
        return
    thread = threading.Thread(target=_return_to_terminal_after_delay, args=(dict(os.environ),), daemon=True)
    thread.start()


def _should_return_to_terminal(args: argparse.Namespace) -> bool:
    return (
        sys.platform == "darwin"
        and not args.no_open_browser
        and not args.no_return_to_terminal
        and terminal_app_from_env(os.environ) is not None
    )


def _return_to_terminal_after_delay(env: dict[str, str]) -> None:
    time.sleep(0.35)
    app_name = terminal_app_from_env(env)
    if app_name is None or sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["open", "-a", app_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def terminal_app_from_env(env: dict[str, str]) -> str | None:
    term_program = env.get("TERM_PROGRAM", "")
    return TERMINAL_APP_BY_TERM_PROGRAM.get(term_program)


def _open_browser(url: str) -> bool:
    open_command = _browser_open_command()
    if open_command:
        try:
            proc = subprocess.run(
                [*open_command, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        return bool(webbrowser.open(url, new=1, autoraise=True))
    except webbrowser.Error:
        return False


def _browser_open_command() -> list[str] | None:
    if sys.platform == "darwin":
        return ["/usr/bin/open"]
    if sys.platform.startswith("linux"):
        return ["xdg-open"]
    return None


def _review_script(nonce: str, total: int) -> str:
    return (
        f"<script nonce='{html.escape(nonce)}'>"
        "(function(){"
        "const total=" + str(total) + ";"
        "const form=document.getElementById('review-form');"
        "const button=document.getElementById('submit-button');"
        "const retryButton=document.getElementById('retry-button');"
        "const hintToggle=document.getElementById('hint-toggle');"
        "const answeredCount=document.getElementById('answered-count');"
        "const dockCount=document.getElementById('dock-count');"
        "const bar=document.getElementById('progress-bar');"
        "function update(){"
        "const checked=[...form.querySelectorAll('input[type=radio]:checked')];"
        "const answered=new Set(checked.map((item)=>item.name));"
        "const count=answered.size;"
        "answeredCount.textContent=String(count);"
        "dockCount.textContent=count+' answered';"
        "bar.style.width=(total?Math.round((count/total)*100):100)+'%';"
        "button.disabled=count!==total;"
        "document.querySelectorAll('[data-progress-item]').forEach((item)=>{"
        "const done=answered.has(item.getAttribute('data-progress-item'));"
        "item.classList.toggle('done',done);"
        "item.querySelector('strong').textContent=done?'Answered':'Pending';"
        "});"
        "}"
        "function resetQuiz(){"
        "form.reset();"
        "if(hintToggle){hintToggle.checked=true;toggleHints();}"
        "update();"
        "const first=document.querySelector('.question');"
        "if(first){first.scrollIntoView({behavior:'smooth',block:'start'});}"
        "}"
        "function toggleHints(){"
        "const show=!hintToggle||hintToggle.checked;"
        "document.querySelectorAll('[data-hint]').forEach((item)=>{"
        "item.hidden=!show;"
        "if(show){item.setAttribute('open','');}"
        "});"
        "}"
        "form.addEventListener('change',update);"
        "if(retryButton){retryButton.addEventListener('click',resetQuiz);}"
        "if(hintToggle){hintToggle.addEventListener('change',toggleHints);}"
        "toggleHints();"
        "update();"
        "})();"
        "</script>"
    )


def _close_script(nonce: str) -> str:
    return (
        f"<script nonce='{html.escape(nonce)}'>"
        "(function(){"
        "const status=document.getElementById('close-status');"
        "const button=document.getElementById('close-browser-button');"
        "function closeReview(){"
        "if(status){status.textContent='Returning to your terminal session. Closing this browser review if your browser allows it.';}"
        "window.open('','_self');"
        "window.close();"
        "}"
        "if(button){button.addEventListener('click',closeReview);}"
        "setTimeout(closeReview,900);"
        "})();"
        "</script>"
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        ":root{color-scheme:dark;--bg:#0b0f12;--surface:#11181a;--surface2:#172023;--line:#2c393d;--line2:#3e5558;--text:#f4f7f5;--muted:#a9b5b1;--soft:#d9c8aa;--accent:#55d6ba;--amber:#f2c45f;--rose:#ff7d73;--blue:#8cb3ff;--ok:#6de091}"
        "*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 50% -20%,#1c2a2b 0,#0b0f12 45%,#090c0e 100%);color:var(--text);font:16px/1.45 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}body:before{content:'';position:fixed;inset:0;pointer-events:none;background:linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px);background-size:36px 36px;mask-image:linear-gradient(#000,transparent 78%)}"
        "main{position:relative;width:min(1180px,calc(100% - 32px));margin:24px auto 120px}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;color:var(--muted)}.topbar strong{display:block;color:var(--text);font-size:19px;line-height:1.1}.topbar span{font-size:13px}.risk{border:1px solid var(--line);border-radius:999px;padding:8px 12px;font-size:12px;font-weight:900;letter-spacing:.06em;text-transform:uppercase}.risk-critical,.risk-high,.result-blocked{border-color:rgba(255,125,115,.6);color:#ffd4cb;background:rgba(255,125,115,.12)}.risk-medium{border-color:rgba(242,196,95,.58);color:#ffe3a2;background:rgba(242,196,95,.12)}.risk-low,.result-passed{border-color:rgba(109,224,145,.58);color:#cdf5d7;background:rgba(109,224,145,.12)}"
        ".hero{border:1px solid var(--line);background:rgba(17,24,26,.94);border-radius:8px;padding:28px;margin-bottom:18px;box-shadow:0 18px 70px rgba(0,0,0,.28)}.hero-copy{max-width:760px}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.08em;font-size:12px;font-weight:900;margin:0 0 8px}h1{margin:0;font-size:clamp(34px,5vw,62px);line-height:1.04;letter-spacing:0}h2{font-size:21px;line-height:1.32;margin:12px 0 18px}.hero p{color:var(--muted);max-width:740px;font-size:17px}.metrics{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}.metrics div{min-width:150px;border-top:1px solid var(--line2);padding-top:10px}.metrics span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.metrics strong{display:block;margin-top:4px;font-size:19px}.trust-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.trust-row span{border:1px solid var(--line);background:#0d1315;color:var(--muted);border-radius:999px;padding:7px 10px;font-size:12px;font-weight:800}"
        ".review-layout{display:grid;grid-template-columns:292px minmax(0,1fr);gap:18px;align-items:start}.progress-panel{position:sticky;top:16px;border:1px solid var(--line);background:rgba(17,24,26,.96);border-radius:8px;padding:16px;box-shadow:0 14px 45px rgba(0,0,0,.24)}.panel-title{font-weight:900;margin-bottom:12px}.progress-track{height:10px;border:1px solid var(--line);border-radius:999px;background:#080d0e;overflow:hidden}.progress-track span{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--amber));transition:width .18s ease}.progress-count{display:flex;align-items:baseline;gap:6px;margin:12px 0 14px}.progress-count strong{font-size:34px;letter-spacing:0}.progress-count span{color:var(--muted)}nav{display:grid;gap:8px}nav a{display:flex;justify-content:space-between;gap:10px;align-items:center;text-decoration:none;color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:10px;background:#0d1315}nav a:focus-visible{outline:2px solid var(--blue);outline-offset:2px}nav a span{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:#202c2e;color:var(--text);font-weight:800}nav a.done{border-color:rgba(85,214,186,.6);color:var(--text)}nav a.done span{background:var(--accent);color:#05110f}.side-note{border-top:1px solid var(--line);margin-top:16px;padding-top:14px}.side-note strong,.side-note span{display:block}.side-note span{color:var(--muted);font-size:13px;margin-top:4px}.hint-toggle{display:flex;align-items:center;gap:10px;margin-top:14px;border:1px solid var(--line);border-radius:8px;padding:10px;background:#0d1315;color:var(--text);font-weight:800}.hint-toggle input{accent-color:var(--accent)}"
        ".question-stack{display:grid;gap:14px}.question{border:1px solid var(--line);background:rgba(17,24,26,.96);border-radius:8px;padding:20px;scroll-margin-top:20px;box-shadow:0 12px 42px rgba(0,0,0,.2)}.question-top{display:flex;justify-content:space-between;gap:12px;align-items:center;color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.05em}.question-index{color:var(--accent);font-weight:900}.dimension-pill{border:1px solid var(--line);border-radius:999px;padding:5px 8px;background:#0d1315;color:var(--soft)}.hint-box{border:1px solid rgba(140,179,255,.34);background:rgba(140,179,255,.08);border-radius:8px;margin:0 0 16px;padding:11px 13px}.hint-box summary{cursor:pointer;color:#cfe0ff;font-weight:900}.hint-box p{margin:8px 0 0;color:var(--muted);font-size:14px}.hint-box[hidden]{display:none}.options{display:grid;gap:10px}.option{display:grid;grid-template-columns:auto auto 1fr;gap:12px;align-items:start;border:1px solid #34474a;border-radius:8px;padding:14px;cursor:pointer;background:#0b1113;transition:border-color .15s ease,background .15s ease,transform .15s ease}.option:hover{border-color:var(--blue);background:#10181b}.option input{position:absolute;opacity:0;pointer-events:none}.option-mark{width:18px;height:18px;border-radius:50%;border:2px solid #748481;margin-top:2px}.option:has(input:focus-visible){outline:2px solid var(--blue);outline-offset:2px}.option:has(input:checked){border-color:var(--accent);background:rgba(85,214,186,.12)}.option:has(input:checked) .option-mark{border-color:var(--accent);box-shadow:inset 0 0 0 4px #0b1113;background:var(--accent)}.option-id{font-weight:900;color:var(--soft);text-transform:uppercase;min-width:1.2rem}.option-text{color:var(--text)}"
        ".submit-dock{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);width:min(840px,calc(100% - 32px));display:flex;justify-content:space-between;align-items:center;gap:16px;border:1px solid var(--line2);background:rgba(10,15,16,.94);backdrop-filter:blur(18px);border-radius:8px;padding:14px 16px;box-shadow:0 16px 60px rgba(0,0,0,.38)}.submit-dock strong{display:block}.submit-dock span{display:block;color:var(--muted);font-size:13px}.dock-actions{display:flex;gap:10px;align-items:center}button{appearance:none;border:0;border-radius:8px;background:var(--accent);color:#04110e;font-weight:900;font-size:15px;padding:13px 18px;cursor:pointer}.secondary-button{border:1px solid var(--line2);background:#121b1d;color:var(--text)}.close-button{width:auto}button:focus-visible{outline:2px solid var(--blue);outline-offset:3px}button:disabled{cursor:not-allowed;opacity:.45;background:#6f817b}.error{border:1px solid var(--rose);color:#ffd8ce;background:#351916;border-radius:8px;padding:12px 14px;margin:12px 0}.note{color:var(--muted);text-align:center}.result{max-width:820px;margin-inline:auto}@media (max-width:840px){main{width:min(100% - 20px,720px);margin-top:14px}.review-layout{grid-template-columns:1fr}.progress-panel{position:static}.metrics{display:grid;grid-template-columns:1fr}.submit-dock{align-items:flex-start;flex-direction:column}.dock-actions{width:100%;display:grid;grid-template-columns:1fr}button{width:100%}.topbar{align-items:flex-start;gap:12px}.question-top{flex-direction:column;align-items:flex-start;gap:8px}.trust-row span{width:100%}}"
        "</style></head><body><main>"
        f"{body}"
        "</main></body></html>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
