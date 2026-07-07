#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from contextlib import suppress
from pathlib import Path
from typing import Any

from owndifflib.common import OwnDiffError, read_json
from owndifflib.mcq import evaluate_answers, write_answers_from_mapping


class UserCanceled(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Answer OwnDiff MCQs in an interactive terminal picker.")
    parser.add_argument("--mcq", default=".owndiff/ownership-mcq.json", help="Public MCQ JSON path.")
    parser.add_argument("--answer-key", default=".owndiff/ownership-answer-key.json", help="Local answer key JSON path.")
    parser.add_argument("--answers-out", default=".owndiff/ownership-answers.json", help="Answers JSON output path.")
    parser.add_argument("--gate-out", default=".owndiff/ownership-gate.json", help="Gate JSON output path.")
    parser.add_argument("--config", help="Optional OwnDiff config override path.")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate answers immediately after writing them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mcq = read_json(Path(args.mcq))
        questions = _questions(mcq)
        if questions and not _has_tty():
            _print_tty_fallback(args.mcq)
            return 2

        answers = _run_tui(mcq) if questions else {}
        answers_payload = write_answers_from_mapping(answers, args.answers_out, args.mcq)
        gate = (
            evaluate_answers(args.mcq, args.answer_key, args.answers_out, args.gate_out, args.config)
            if args.evaluate
            else None
        )
    except UserCanceled:
        print("canceled: no OwnDiff answers were written", file=sys.stderr)
        return 130
    except (OwnDiffError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("canceled: no OwnDiff answers were written", file=sys.stderr)
        return 130

    payload = {
        "answers_out": str(Path(args.answers_out)),
        "answers": answers_payload["answers"],
    }
    if gate is not None:
        payload.update(
            {
                "gate_out": str(Path(args.gate_out)),
                "status": gate["status"],
                "score_percent": gate["score_percent"],
                "agent_may_push_merge_request": gate["agent_may_push_merge_request"],
            }
        )
    print(json.dumps(payload, sort_keys=True))
    if gate is None:
        return 0
    return 0 if gate["agent_may_push_merge_request"] else 3


def _questions(mcq: dict[str, Any]) -> list[dict[str, Any]]:
    return [question for question in mcq.get("questions", []) if isinstance(question, dict)]


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb"


def _print_tty_fallback(mcq_path: str) -> None:
    print(
        "error: quiz_tui.py requires an interactive terminal (TTY).\n"
        "Fallback for coding-agent chats:\n"
        f"  python3 /path/to/owndiff/scripts/present_mcq.py --mcq {mcq_path}\n"
        "Then submit the human selections with submit_answers.py --evaluate.",
        file=sys.stderr,
    )


def _run_tui(mcq: dict[str, Any]) -> dict[str, str]:
    try:
        import curses
    except ImportError as exc:
        raise OwnDiffError("Terminal picker requires Python curses support. Use present_mcq.py instead.") from exc

    try:
        return curses.wrapper(lambda screen: _quiz_loop(curses, screen, mcq))
    except curses.error as exc:
        raise OwnDiffError("Terminal picker could not initialize this TTY. Use present_mcq.py instead.") from exc


def _quiz_loop(curses: Any, screen: Any, mcq: dict[str, Any]) -> dict[str, str]:
    with suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    with suppress(curses.error):
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    _init_colors(curses)

    questions = _questions(mcq)
    selected: dict[str, str] = {}
    index = 0
    focus = 0
    review = False
    zones: list[tuple[int, int]] = []

    while True:
        if review:
            _draw_review(curses, screen, mcq, questions, selected)
            key = screen.get_wch()
            if _is_quit(key):
                raise UserCanceled()
            if _is_previous(curses, key):
                review = False
                index = max(0, len(questions) - 1)
                focus = _selected_index(questions[index], selected)
                continue
            if isinstance(key, str) and key.lower() == "s":
                if len(selected) == len(questions):
                    return selected
                curses.beep()
            continue

        question = questions[index]
        options = _options(question)
        if not options:
            raise OwnDiffError(f"Question {question.get('id', index + 1)} has no answer options")
        focus = max(0, min(focus, len(options) - 1))
        zones = _draw_question(curses, screen, mcq, questions, selected, index, focus)
        key = screen.get_wch()

        if _is_quit(key):
            raise UserCanceled()
        if _is_next(curses, key) and str(question.get("id")) in selected:
            if index == len(questions) - 1:
                review = True
            else:
                index += 1
                focus = _selected_index(questions[index], selected)
            continue
        if _is_previous(curses, key):
            if index > 0:
                index -= 1
                focus = _selected_index(questions[index], selected)
            continue
        if _is_down(curses, key):
            focus = (focus + 1) % len(options)
            continue
        if _is_up(curses, key):
            focus = (focus - 1) % len(options)
            continue
        if _is_enter(key):
            selected[str(question.get("id"))] = str(options[focus].get("id", "")).lower()
            if index == len(questions) - 1:
                review = True
            else:
                index += 1
                focus = _selected_index(questions[index], selected)
            continue
        if key == curses.KEY_MOUSE:
            clicked = _mouse_option(curses, zones)
            if clicked is not None:
                focus = clicked
                selected[str(question.get("id"))] = str(options[focus].get("id", "")).lower()
                if index == len(questions) - 1:
                    review = True
                else:
                    index += 1
                    focus = _selected_index(questions[index], selected)
            continue
        if isinstance(key, str):
            option_index = _option_index_for_key(options, key)
            if option_index is not None:
                focus = option_index
                selected[str(question.get("id"))] = str(options[focus].get("id", "")).lower()
                if index == len(questions) - 1:
                    review = True
                else:
                    index += 1
                    focus = _selected_index(questions[index], selected)
                continue
            if key.lower() == "s" and len(selected) == len(questions):
                review = True


def _init_colors(curses: Any) -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_BLUE, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)
        curses.init_pair(7, curses.COLOR_WHITE, -1)
    except curses.error:
        pass


def _draw_question(
    curses: Any,
    screen: Any,
    mcq: dict[str, Any],
    questions: list[dict[str, Any]],
    selected: dict[str, str],
    index: int,
    focus: int,
) -> list[tuple[int, int]]:
    screen.erase()
    height, width = screen.getmaxyx()
    if _too_small(screen):
        return []

    question = questions[index]
    question_id = str(question.get("id"))
    options = _options(question)
    risk = str(mcq.get("risk_level") or "unknown")
    answered = len(selected)
    total = len(questions)

    _draw_header(curses, screen, "Ownership Gate", risk, answered, total, index)

    content_top = 7
    content_left = 2
    content_bottom = height - 4
    content_right = width - 3
    has_side_panel = width >= 112
    main_right = content_right - 35 if has_side_panel else content_right
    _draw_box(screen, content_top, content_left, content_bottom, main_right, "4 Generate ownership questions", _color(curses, 2))
    if has_side_panel:
        _draw_gate_panel(curses, screen, content_top, main_right + 2, content_bottom, content_right, risk, answered, total, question)

    row = content_top + 2
    question_label = f"Question {index + 1} of {total}"
    _add(screen, row, content_left + 3, question_label, curses.A_BOLD | _color(curses, 3))
    if str(question.get("dimension") or ""):
        dimension = _truncate(f"Dimension: {question.get('dimension')}", main_right - content_left - 32)
        _add(screen, row, main_right - len(dimension) - 2, dimension, _color(curses, 2))
    row += 2

    title = f"{question_id}. {question.get('question', '')}"
    for line in textwrap.wrap(title, max(20, main_right - content_left - 8)):
        _add(screen, row, content_left + 3, line, curses.A_BOLD)
        row += 1
    row += 1

    zones: list[tuple[int, int]] = []
    current_answer = selected.get(question_id)
    for option_index, option in enumerate(options):
        option_id = str(option.get("id", "")).lower()
        is_selected = current_answer == option_id
        is_focused = option_index == focus
        option_text = str(option.get("text", ""))
        option_width = max(20, main_right - content_left - 28)
        wrapped = textwrap.wrap(option_text, option_width) or [""]
        option_bottom = row + len(wrapped) - 1
        if option_bottom >= content_bottom - 1:
            _add(
                screen,
                content_bottom - 1,
                content_left + 3,
                "More content is available; enlarge the terminal to see every option.",
                _color(curses, 3),
            )
            break
        zones.append((row, option_bottom))
        _draw_option_card(
            curses,
            screen,
            row,
            content_left + 3,
            option_bottom,
            main_right - 3,
            option_id,
            wrapped,
            is_focused=is_focused,
            is_selected=is_selected,
        )
        row = option_bottom + 1

    _draw_footer(curses, screen, len(selected), total)
    screen.refresh()
    return zones


def _draw_review(
    curses: Any,
    screen: Any,
    mcq: dict[str, Any],
    questions: list[dict[str, Any]],
    selected: dict[str, str],
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if _too_small(screen):
        return

    risk = str(mcq.get("risk_level") or "unknown")
    total = len(questions)
    _draw_header(curses, screen, "Review", risk, len(selected), total, total - 1)

    content_top = 7
    content_left = 2
    content_bottom = height - 4
    content_right = width - 3
    has_side_panel = width >= 112
    main_right = content_right - 35 if has_side_panel else content_right
    _draw_box(screen, content_top, content_left, content_bottom, main_right, "5 Human verification", _color(curses, 2))
    if has_side_panel:
        _draw_gate_panel(curses, screen, content_top, main_right + 2, content_bottom, content_right, risk, len(selected), total, None)

    row = content_top + 2
    _add(screen, row, content_left + 3, "Confirm every selected answer before OwnDiff evaluates the local gate.", curses.A_BOLD)
    row += 2
    for question_index, question in enumerate(questions):
        question_id = str(question.get("id"))
        answer = selected.get(question_id, "(missing)")
        option_text = _selected_option_text(question, answer)
        status = "ANSWERED" if answer != "(missing)" else "MISSING"
        line = f"{question_id:<4} answer {answer:<3}  {status}"
        attr = curses.A_BOLD | (_color(curses, 1) if answer != "(missing)" else _color(curses, 3))
        _add(screen, row, content_left + 3, line, attr)
        row += 1
        for wrapped in textwrap.wrap(option_text, max(20, main_right - content_left - 8)):
            _add(screen, row, content_left + 7, wrapped)
            row += 1
        row += 1
        if row >= content_bottom - 1 and question_index < len(questions) - 1:
            _add(screen, content_bottom - 1, content_left + 3, "More answers are available; enlarge the terminal to see everything.", _color(curses, 3))
            break

    if len(selected) == len(questions):
        footer = "All questions answered. Press S to submit or P to edit."
    else:
        footer = "Some answers are missing. Press P to return to the quiz."
    _draw_footer(curses, screen, len(selected), total, footer)
    screen.refresh()


def _too_small(screen: Any) -> bool:
    height, width = screen.getmaxyx()
    if height >= 20 and width >= 76:
        return False
    screen.erase()
    _add(screen, 0, 0, "Terminal too small for OwnDiff picker. Resize to at least 76x20.")
    screen.refresh()
    return True


def _draw_header(
    curses: Any,
    screen: Any,
    title: str,
    risk: str,
    answered: int,
    total: int,
    index: int,
) -> None:
    _, width = screen.getmaxyx()
    left = 2
    right = width - 3
    _draw_box(screen, 0, left, 5, right, None, _color(curses, 2))
    brand = "OwnDiff"
    _add(screen, 1, left + 3, brand, curses.A_BOLD | _color(curses, 1))
    _add(screen, 1, left + 13, title, curses.A_BOLD | _color(curses, 2))
    risk_text = f"RISK {risk.upper()}"
    position = f"Q{min(index + 1, total)}/{total}" if total else "Q0/0"
    status = f"{answered}/{total} answered"
    gate = "READY TO REVIEW" if total and answered == total else "GATE LOCKED"
    meta = f"{risk_text} | {status} | {position} | {gate}"
    _add(screen, 1, right - len(meta) - 2, meta, curses.A_BOLD | _risk_attr(curses, risk))

    tagline = "Local human-ownership check before risky pushes or merge requests."
    _add(screen, 2, left + 3, _truncate(tagline, right - left - 6), _color(curses, 7))

    bar_width = max(12, min(38, width - 56))
    bar = _progress_bar(answered, total, bar_width)
    _add(screen, 3, left + 3, f"Gate progress {bar}", _color(curses, 1 if answered == total else 3))
    status_note = "No push/MR until the human proves understanding."
    _add(screen, 3, right - len(status_note) - 2, status_note, _color(curses, 3))
    _draw_stage_bar(curses, screen, 4, left + 3, right - 2, risk, answered, total)


def _draw_footer(curses: Any, screen: Any, answered: int, total: int, message: str | None = None) -> None:
    height, width = screen.getmaxyx()
    left = 2
    right = width - 3
    row = height - 3
    _draw_box(screen, row, left, height - 1, right, None, _color(curses, 2))
    if message is None:
        if answered == total:
            message = "All answered. Press S to review and submit."
        else:
            message = "Move arrows/j/k  Answer A-D or Enter  Back/next P/N  Mouse click  Quit Q"
    _add(screen, row + 1, left + 3, _truncate(message, right - left - 8), curses.A_BOLD)


def _draw_stage_bar(
    curses: Any,
    screen: Any,
    row: int,
    left: int,
    right: int,
    risk: str,
    answered: int,
    total: int,
) -> None:
    gate = "review" if total and answered == total else "locked"
    parts = [
        ("1 Analyze git diff", "done", _color(curses, 1)),
        ("2 Score risky areas", risk.lower(), _risk_attr(curses, risk)),
        ("3 Ownership MCQs", f"{answered}/{total}", _color(curses, 2)),
        ("4 Local gate", gate, _color(curses, 1 if gate == "review" else 3)),
    ]
    col = left
    for index, (label, value, attr) in enumerate(parts):
        text = f"{label} [{value}]"
        if col + len(text) >= right:
            _add(screen, row, col, _truncate(text, right - col), attr | curses.A_BOLD)
            return
        _add(screen, row, col, text, attr | curses.A_BOLD)
        col += len(text)
        if index < len(parts) - 1 and col + 4 < right:
            _add(screen, row, col, " -> ", _color(curses, 7))
            col += 4


def _draw_gate_panel(
    curses: Any,
    screen: Any,
    top: int,
    left: int,
    bottom: int,
    right: int,
    risk: str,
    answered: int,
    total: int,
    question: dict[str, Any] | None,
) -> None:
    _draw_box(screen, top, left, bottom, right, "Gate status", _color(curses, 2))
    row = top + 2
    state = "READY TO SUBMIT" if total and answered == total else "LOCKED"
    state_attr = _color(curses, 1 if state != "LOCKED" else 3) | curses.A_BOLD
    _add(screen, row, left + 3, state, state_attr)
    row += 1
    _add(screen, row, left + 3, _truncate(f"Risk: {risk.upper()}", right - left - 6), _risk_attr(curses, risk) | curses.A_BOLD)
    row += 2

    _add(screen, row, left + 3, "Human must cover:", curses.A_BOLD | _color(curses, 2))
    row += 1
    current_dimension = str((question or {}).get("dimension") or "").lower()
    for item in ["behavior", "blast radius", "failure modes", "tests", "rollback"]:
        marker = ">" if current_dimension and current_dimension in item else "-"
        attr = _color(curses, 3) if marker == ">" else _color(curses, 7)
        _add(screen, row, left + 4, _truncate(f"{marker} {item}", right - left - 8), attr | (curses.A_BOLD if marker == ">" else 0))
        row += 1
        if row >= bottom - 2:
            break

    if row + 3 < bottom:
        row += 1
        _add(screen, row, left + 3, "Local evidence:", curses.A_BOLD | _color(curses, 2))
        row += 1
        for artifact in [".owndiff/ownership-mcq.json", ".owndiff/ownership-gate.json"]:
            _add(screen, row, left + 4, _truncate(artifact, right - left - 8), _color(curses, 7))
            row += 1
            if row >= bottom:
                break


def _draw_option_card(
    curses: Any,
    screen: Any,
    top: int,
    left: int,
    bottom: int,
    right: int,
    option_id: str,
    wrapped_text: list[str],
    *,
    is_focused: bool,
    is_selected: bool,
) -> None:
    attr = _option_attr(curses, is_focused=is_focused, is_selected=is_selected)
    badge = f" {option_id.upper()} "
    marker = "SELECTED" if is_selected else "FOCUS" if is_focused else f"KEY {option_id.upper()}"
    prefix = ">" if is_focused else " "
    _add(screen, top, left, prefix, attr | curses.A_BOLD)
    _add(screen, top, left + 2, badge, curses.A_BOLD | attr | _color(curses, 1 if is_selected else 2))
    text_col = left + 8
    marker_col = max(text_col + 8, right - len(marker) - 2)
    first_width = max(10, marker_col - text_col - 2)
    _add(screen, top, text_col, _truncate(wrapped_text[0], first_width), attr)
    _add(screen, top, marker_col, marker, curses.A_BOLD | attr | _color(curses, 1 if is_selected else 3 if is_focused else 7))
    for offset, line in enumerate(wrapped_text[1:], start=1):
        _add(screen, top + offset, text_col, _truncate(line, right - text_col - 2), attr)


def _draw_box(screen: Any, top: int, left: int, bottom: int, right: int, title: str | None, attr: int = 0) -> None:
    if bottom <= top or right <= left:
        return
    width = right - left - 1
    _add(screen, top, left, "+" + "-" * width + "+", attr)
    for row in range(top + 1, bottom):
        _add(screen, row, left, "|", attr)
        _add(screen, row, right, "|", attr)
    _add(screen, bottom, left, "+" + "-" * width + "+", attr)
    if title:
        _add(screen, top, left + 3, _truncate(f" {title} ", width - 4), attr)


def _progress_bar(answered: int, total: int, width: int) -> str:
    if total <= 0:
        percent = 100
        filled = width
    else:
        percent = round((answered / total) * 100)
        filled = round((answered / total) * width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {percent:>3}%"


def _risk_attr(curses: Any, risk: str) -> int:
    risk = risk.lower()
    if risk in {"critical", "high"}:
        return _color(curses, 3)
    if risk == "medium":
        return _color(curses, 2)
    return _color(curses, 1)


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _add(screen: Any, row: int, col: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    max_length = width - col - 1
    if row < 0 or row >= height or col < 0 or max_length <= 0:
        return
    try:
        screen.addnstr(row, col, text, max_length, attr)
    except Exception as exc:
        if exc.__class__.__name__ != "error":
            raise


def _options(question: dict[str, Any]) -> list[dict[str, Any]]:
    return [option for option in question.get("options", []) if isinstance(option, dict)]


def _selected_index(question: dict[str, Any], selected: dict[str, str]) -> int:
    answer = selected.get(str(question.get("id")))
    for index, option in enumerate(_options(question)):
        if str(option.get("id", "")).lower() == answer:
            return index
    return 0


def _option_index_for_key(options: list[dict[str, Any]], key: str) -> int | None:
    pressed = key.lower()
    for index, option in enumerate(options):
        if str(option.get("id", "")).lower() == pressed:
            return index
    return None


def _selected_option_text(question: dict[str, Any], answer: str) -> str:
    for option in _options(question):
        if str(option.get("id", "")).lower() == answer:
            return str(option.get("text", ""))
    return ""


def _mouse_option(curses: Any, zones: list[tuple[int, int]]) -> int | None:
    try:
        _, _, y, _, state = curses.getmouse()
    except curses.error:
        return None
    if not state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED):
        return None
    for index, (start, end) in enumerate(zones):
        if start <= y <= end:
            return index
    return None


def _is_quit(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in {"q", "\x1b"}


def _is_enter(key: Any) -> bool:
    return key in {"\n", "\r"}


def _is_down(curses: Any, key: Any) -> bool:
    return key == curses.KEY_DOWN or (isinstance(key, str) and key.lower() == "j")


def _is_up(curses: Any, key: Any) -> bool:
    return key == curses.KEY_UP or (isinstance(key, str) and key.lower() == "k")


def _is_next(curses: Any, key: Any) -> bool:
    return key == curses.KEY_RIGHT or (isinstance(key, str) and key.lower() == "n")


def _is_previous(curses: Any, key: Any) -> bool:
    return key == curses.KEY_LEFT or (isinstance(key, str) and key.lower() == "p")


def _color(curses: Any, pair: int) -> int:
    if not curses.has_colors():
        return 0
    return curses.color_pair(pair)


def _option_attr(curses: Any, is_focused: bool, is_selected: bool) -> int:
    attr = 0
    if is_selected:
        attr |= curses.A_BOLD | _color(curses, 1)
    if is_focused:
        attr |= curses.A_REVERSE
    return attr


if __name__ == "__main__":
    raise SystemExit(main())
