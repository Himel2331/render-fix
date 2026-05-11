#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import re
import urllib.parse
import sys
from contextlib import closing, suppress
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputTextMessageContent, Poll, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, InlineQueryHandler
from telegram import InlineQueryResultArticle

BASE_PATH = Path(__file__).resolve().with_name("bot_base.py")
spec = importlib.util.spec_from_file_location("bot_base", BASE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load bot_base.py from {BASE_PATH}")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)


# ============================================================
# Advanced overlay: feasible additions without OCR / paid APIs
# ============================================================

CHECKMARKS = ("✅", "☑", "✔", "✓")
TEXT_IMPORT_STATES = {"adv_await_import_text", "adv_await_clone_source"}
SPEED_PRESETS = {
    "slow": (1.50, "slow"),
    "normal": (1.00, "normal"),
    "fast": (0.75, "fast"),
}
OPTION_RE = re.compile(r"^\s*(?:[-*•]|\(?[A-Ja-j1-9]\)|[A-Ja-j1-9][\).:-])\s*(.+?)\s*$")
ANSWER_RE = re.compile(r"^\s*(?:answer|ans|correct|right)\s*[:\-]\s*(.+?)\s*$", re.I)
EXPL_RE = re.compile(r"^\s*(?:explanation|explain|reason|note)\s*[:\-]\s*(.+?)\s*$", re.I)
QUESTION_PREFIX_RE = re.compile(r"^\s*(?:Q(?:uestion)?\s*\d+|\d+)\s*[\).:\-]\s*", re.I)
COUNTER_RE = re.compile(r"^\s*[\[(]?\s*\d+\s*/\s*\d+\s*[\])]?\s*", re.I)
URL_RE = re.compile(r"(?:https?://\S+|t\.me/\S+)", re.I)
USERNAME_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}")
QUIZBOT_TOKEN_RE = re.compile(r"(?:@quizbot\s+)?quiz\s*:\s*([A-Za-z0-9_-]{4,})", re.I)


def ensure_column(table: str, column: str, definition: str) -> None:
    with closing(base.DBH.connect()) as conn:
        cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()


base.DBH.executescript(
    """
    CREATE TABLE IF NOT EXISTS draft_sections (
        draft_id TEXT NOT NULL,
        section_no INTEGER NOT NULL,
        title TEXT NOT NULL,
        start_q INTEGER NOT NULL,
        end_q INTEGER NOT NULL,
        question_time INTEGER,
        PRIMARY KEY (draft_id, section_no)
    );

    CREATE TABLE IF NOT EXISTS clone_sessions (
        user_id INTEGER PRIMARY KEY,
        draft_id TEXT NOT NULL,
        clone_token TEXT,
        source_text TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """
)
ensure_column("sessions", "speed_factor", "REAL DEFAULT 1.0")
ensure_column("sessions", "speed_mode", "TEXT DEFAULT 'normal'")
ensure_column("sessions", "paused_at", "INTEGER")
ensure_column("session_questions", "section_title", "TEXT")
ensure_column("session_questions", "question_time_override", "INTEGER")


base._FINAL_SUPPORTED_GROUP_COMMANDS = set(getattr(base, "_FINAL_SUPPORTED_GROUP_COMMANDS", set())) | {
    "pauseq",
    "resumeq",
    "skipq",
    "speed",
}


def clean_forwarded_text(text: str) -> str:
    value = base.normalize_visual_text(text or "")
    value = urllib.parse.unquote(value)
    value = COUNTER_RE.sub("", value)
    value = re.sub(r"\bvia\b\s+@?[A-Za-z0-9_]+", " ", value, flags=re.I)
    value = URL_RE.sub(" ", value)
    value = USERNAME_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -–—|•")



def _strip_checkmark(text: str) -> Tuple[str, bool]:
    raw = text or ""
    marked = any(mark in raw for mark in CHECKMARKS)
    for mark in CHECKMARKS:
        raw = raw.replace(mark, "")
    return base.normalize_visual_text(raw), marked



def question_signature(question: str, options: Iterable[str]) -> str:
    merged = " || ".join([clean_forwarded_text(question)] + [clean_forwarded_text(x) for x in options])
    merged = merged.casefold()
    merged = re.sub(r"\s+", " ", merged)
    return merged.strip()



def existing_question_signatures(draft_id: str) -> set[str]:
    seen: set[str] = set()
    for row in base.get_draft_questions(draft_id):
        opts = base.jload(row["options"], []) or []
        seen.add(question_signature(str(row["question"]), [str(x) for x in opts]))
    return seen



def dedup_add_question_to_draft(draft_id: str, question: str, options: List[str], correct_option: int, explanation: str, src: str) -> Tuple[bool, Optional[int]]:
    sig = question_signature(question, options)
    if sig in existing_question_signatures(draft_id):
        return False, None
    q_no = base.add_question_to_draft(draft_id, clean_forwarded_text(question), [clean_forwarded_text(o) for o in options], int(correct_option), clean_forwarded_text(explanation), src)
    return True, q_no



def parse_answer_ref(ref: str, options: List[str]) -> Optional[int]:
    raw = base.normalize_visual_text(ref or "")
    if not raw:
        return None
    raw_up = raw.upper()
    if len(raw_up) == 1 and "A" <= raw_up <= "J":
        idx = ord(raw_up) - ord("A")
        if idx < len(options):
            return idx
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return idx
    for idx, opt in enumerate(options):
        if base.normalize_visual_text(opt).casefold() == raw.casefold():
            return idx
    return None



def parse_marked_questions_from_text(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").replace("\r", "")
    raw = raw.strip()
    if not raw:
        return []

    # JSON array support
    try:
        payload = json.loads(raw)
        if isinstance(payload, list):
            items: List[Dict[str, Any]] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                q = clean_forwarded_text(str(item.get("question") or item.get("questions") or ""))
                opts = item.get("options") or []
                if isinstance(opts, dict):
                    opts = list(opts.values())
                opts = [clean_forwarded_text(str(x)) for x in opts if str(x).strip()]
                ans = parse_answer_ref(str(item.get("answer") or item.get("correct") or ""), opts)
                if q and len(opts) >= 2 and ans is not None:
                    items.append({
                        "question": q,
                        "options": opts,
                        "correct_option": ans,
                        "explanation": clean_forwarded_text(str(item.get("explanation") or "")),
                    })
            if items:
                return items
    except Exception:
        pass

    blocks = [b.strip() for b in re.split(r"\n\s*\n+", raw) if b.strip()]
    parsed: List[Dict[str, Any]] = []

    for block in blocks:
        lines = [base.normalize_visual_text(x) for x in block.split("\n") if base.normalize_visual_text(x)]
        if not lines:
            continue
        question_parts: List[str] = []
        options: List[str] = []
        answer_ref: Optional[str] = None
        explanation_parts: List[str] = []
        correct_option: Optional[int] = None

        for idx, line in enumerate(lines):
            ans_m = ANSWER_RE.match(line)
            if ans_m:
                answer_ref = ans_m.group(1).strip()
                continue
            expl_m = EXPL_RE.match(line)
            if expl_m:
                explanation_parts.append(expl_m.group(1).strip())
                continue
            opt_m = OPTION_RE.match(line)
            if opt_m:
                opt_text, marked = _strip_checkmark(opt_m.group(1).strip())
                if opt_text:
                    options.append(opt_text)
                    if marked:
                        correct_option = len(options) - 1
                continue
            if idx == 0 and not options:
                q_line = clean_forwarded_text(QUESTION_PREFIX_RE.sub("", line))
                if q_line:
                    question_parts.append(q_line)
                continue
            if options:
                # treat trailing free text as explanation or option continuation
                if explanation_parts:
                    explanation_parts.append(line)
                elif options:
                    options[-1] = base.normalize_visual_text(f"{options[-1]} {line}")
                continue
            question_parts.append(line)

        question = clean_forwarded_text(" ".join(question_parts))
        if correct_option is None and answer_ref is not None:
            correct_option = parse_answer_ref(answer_ref, options)
        if question and len(options) >= 2 and correct_option is not None:
            parsed.append(
                {
                    "question": question,
                    "options": options,
                    "correct_option": int(correct_option),
                    "explanation": clean_forwarded_text(" ".join(explanation_parts)),
                }
            )

    return parsed



def resolve_editable_draft(user_id: int, raw_code: str) -> Optional[Any]:
    code = base.normalize_visual_text(raw_code or "").upper()
    draft_id = code or (base.get_active_draft_id(user_id) or "")
    if not draft_id:
        return None
    draft = base.get_draft(draft_id)
    if not draft:
        return None
    if int(draft["owner_id"]) != user_id and not getattr(base, "is_all_access_admin", lambda _x: False)(user_id):
        return None
    return draft



def list_sections(draft_id: str) -> List[Any]:
    return base.DBH.fetchall("SELECT * FROM draft_sections WHERE draft_id=? ORDER BY section_no ASC", (draft_id,))



def set_section(draft_id: str, start_q: int, end_q: int, title: str, question_time: Optional[int]) -> None:
    next_no_row = base.DBH.fetchone("SELECT COALESCE(MAX(section_no), 0) AS mx FROM draft_sections WHERE draft_id=?", (draft_id,))
    next_no = int(next_no_row["mx"] if next_no_row else 0) + 1
    base.DBH.execute(
        "INSERT INTO draft_sections(draft_id, section_no, title, start_q, end_q, question_time) VALUES(?,?,?,?,?,?)",
        (draft_id, next_no, base.normalize_visual_text(title), int(start_q), int(end_q), int(question_time) if question_time else None),
    )



def clear_sections(draft_id: str) -> None:
    base.DBH.execute("DELETE FROM draft_sections WHERE draft_id=?", (draft_id,))



def apply_sections_to_session(session_id: str, draft_id: str) -> None:
    for row in list_sections(draft_id):
        base.DBH.execute(
            "UPDATE session_questions SET section_title=?, question_time_override=? WHERE session_id=? AND q_no BETWEEN ? AND ?",
            (
                row["title"],
                row["question_time"],
                session_id,
                int(row["start_q"]),
                int(row["end_q"]),
            ),
        )



def extract_clone_token(text: str) -> Optional[str]:
    raw = urllib.parse.unquote(base.normalize_visual_text(text or ""))
    m = QUIZBOT_TOKEN_RE.search(raw)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\b)quiz[:=]([A-Za-z0-9_-]{4,})", raw, flags=re.I)
    if m:
        return m.group(1)
    return None



def start_clone_session(user_id: int, draft_id: str, clone_token: str, source_text: str) -> None:
    base.DBH.execute(
        "INSERT OR REPLACE INTO clone_sessions(user_id, draft_id, clone_token, source_text, active, created_at, updated_at) VALUES(?,?,?,?,1,COALESCE((SELECT created_at FROM clone_sessions WHERE user_id=?),?),?)",
        (user_id, draft_id, clone_token, source_text, user_id, base.now_ts(), base.now_ts()),
    )



def get_clone_session(user_id: int) -> Optional[Any]:
    return base.DBH.fetchone("SELECT * FROM clone_sessions WHERE user_id=? AND active=1", (user_id,))



def stop_clone_session(user_id: int) -> None:
    base.DBH.execute("DELETE FROM clone_sessions WHERE user_id=?", (user_id,))



def format_draft_info(draft: Any) -> str:
    q_rows = base.get_draft_questions(draft["id"])
    sections = list_sections(draft["id"])
    lines = [
        f"<b>Draft Info</b>",
        f"Title: <b>{base.html_escape(draft['title'])}</b>",
        f"Code: <code>{draft['id']}</code>",
        f"Owner: <code>{draft['owner_id']}</code>",
        f"Questions: <b>{len(q_rows)}</b>",
        f"Time / question: <b>{draft['question_time']} sec</b>",
        f"Negative / wrong: <b>{draft['negative_mark']}</b>",
        f"Created: <b>{base.fmt_dt(draft['created_at'])}</b>",
        f"Updated: <b>{base.fmt_dt(draft['updated_at'])}</b>",
    ]
    if sections:
        lines.append("")
        lines.append("<b>Sections</b>")
        for row in sections:
            lines.append(
                f"• {base.html_escape(row['title'])} — Q{row['start_q']}-Q{row['end_q']}"
                + (f" — {row['question_time']} sec" if row["question_time"] else "")
            )
    return "\n".join(lines)



def delete_question_numbers(draft_id: str, q_numbers: List[int]) -> int:
    if not q_numbers:
        return 0
    with closing(base.DBH.connect()) as conn:
        removed = 0
        for q_no in sorted(set(int(x) for x in q_numbers), reverse=True):
            cur = conn.execute("DELETE FROM draft_questions WHERE draft_id=? AND q_no=?", (draft_id, q_no))
            removed += int(cur.rowcount or 0)
        rows = conn.execute("SELECT id, q_no FROM draft_questions WHERE draft_id=? ORDER BY q_no ASC", (draft_id,)).fetchall()
        for new_no, row in enumerate(rows, start=1):
            conn.execute("UPDATE draft_questions SET q_no=? WHERE id=?", (new_no, row["id"]))
        conn.commit()
    base.refresh_draft_status(draft_id)
    return removed



def parse_q_number_list(raw: str) -> List[int]:
    out: List[int] = []
    for part in re.split(r"\s*,\s*", raw.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                x, y = int(a), int(b)
                if x <= y:
                    out.extend(list(range(x, y + 1)))
            continue
        if part.isdigit():
            out.append(int(part))
    return sorted(set(out))



def shuffle_draft_questions(draft_id: str) -> None:
    rows = [dict(r) for r in base.get_draft_questions(draft_id)]
    if len(rows) < 2:
        return
    random.shuffle(rows)
    with closing(base.DBH.connect()) as conn:
        conn.execute("DELETE FROM draft_questions WHERE draft_id=?", (draft_id,))
        for idx, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO draft_questions(draft_id, q_no, question, options, correct_option, explanation, src) VALUES(?,?,?,?,?,?,?)",
                (
                    draft_id,
                    idx,
                    row["question"],
                    row["options"],
                    row["correct_option"],
                    row["explanation"],
                    row["src"],
                ),
            )
        conn.commit()
    base.refresh_draft_status(draft_id)



def copy_draft(draft_id: str, owner_id: int) -> str:
    draft = base.get_draft(draft_id)
    if not draft:
        raise ValueError("Draft not found")
    new_id = base.create_draft(owner_id, f"{draft['title']} (Copy)", int(draft['question_time']), float(draft['negative_mark']))
    for row in base.get_draft_questions(draft_id):
        base.add_question_to_draft(
            new_id,
            str(row["question"]),
            [str(x) for x in (base.jload(row["options"], []) or [])],
            int(row["correct_option"]),
            str(row["explanation"] or ""),
            str(row["src"] or "copy"),
        )
    for row in list_sections(draft_id):
        set_section(new_id, int(row["start_q"]), int(row["end_q"]), str(row["title"]), int(row["question_time"]) if row["question_time"] else None)
    return new_id


async def import_text_into_draft(message, context, draft_id: str, text: str, src: str = "text") -> None:
    parsed = parse_marked_questions_from_text(text)
    if not parsed:
        await base.safe_reply(
            message,
            "No valid questions were found. Supported format: one question block with options, and the correct option marked with ✅ or an Answer: line.",
        )
        return
    added = 0
    skipped = 0
    for item in parsed:
        ok, _q_no = dedup_add_question_to_draft(
            draft_id,
            item["question"],
            list(item["options"]),
            int(item["correct_option"]),
            str(item.get("explanation") or ""),
            src,
        )
        if ok:
            added += 1
        else:
            skipped += 1
    draft = base.get_draft(draft_id)
    await base.send_draft_card(
        context,
        message.chat.id,
        message.from_user.id,
        draft_id,
        header=f"✅ Text import complete. Added: {added} | Skipped duplicates: {skipped}",
    )
    if draft:
        base.audit(message.from_user.id, "import_text", draft_id, {"added": added, "skipped": skipped})


_previous_create_session_from_draft = base.create_session_from_draft

def create_session_from_draft(draft_id: str, chat_id: int, actor_id: int) -> Optional[str]:
    session_id = _previous_create_session_from_draft(draft_id, chat_id, actor_id)
    if session_id:
        apply_sections_to_session(session_id, draft_id)
        base.DBH.execute(
            "UPDATE sessions SET speed_factor=COALESCE(speed_factor, 1.0), speed_mode=COALESCE(speed_mode, 'normal'), paused_at=NULL WHERE id=?",
            (session_id,),
        )
    return session_id


base.create_session_from_draft = create_session_from_draft


async def begin_or_advance_exam(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session or session["status"] != "running":
        return
    next_index = int(session["current_index"] or 0) + 1
    total = int(session["total_questions"] or 0)
    if next_index > total:
        await base.finish_exam(context, session_id, reason="completed")
        return
    q = base.get_session_question(session_id, next_index)
    if not q:
        await base.finish_exam(context, session_id, reason="missing_question")
        return
    options = base.jload(q["options"], []) or []
    section_title = base.normalize_visual_text(q["section_title"] or "")
    base_seconds = int(q["question_time_override"] or session["question_time"] or 30)
    speed_factor = float(session["speed_factor"] or 1.0)
    effective_seconds = max(5, int(round(base_seconds * speed_factor)))

    try:
        prefix_parts = [f"[{next_index}/{total}]"]
        if section_title:
            prefix_parts.append(f"[{section_title}]")
        prefix_parts.append(f"[{base.normalize_visual_text(session['title'])}]")
        question_prefix = " ".join(prefix_parts) + "\n"
        poll_question = (question_prefix + str(q["question"])).strip()
        if len(poll_question) > 300:
            allowed_q = max(10, 300 - len(question_prefix))
            poll_question = question_prefix + str(q["question"])[: allowed_q - 1].rstrip() + "…"
        explanation_text = base.normalize_visual_text(q["explanation"] or f"Question {next_index} of {total}")
        if len(explanation_text) > 200:
            explanation_text = explanation_text[:199] + "…"
        msg = await context.bot.send_poll(
            chat_id=session["chat_id"],
            question=poll_question,
            options=options,
            type=Poll.QUIZ,
            is_anonymous=False,
            allows_multiple_answers=False,
            correct_option_id=int(q["correct_option"]),
            explanation=explanation_text,
            open_period=effective_seconds,
        )
    except TelegramError as exc:
        base.logger.exception("Failed to send poll: %s", exc)
        await base.finish_exam(context, session_id, reason="send_poll_error")
        return

    poll_id = msg.poll.id
    with closing(base.DBH.connect()) as conn:
        conn.execute(
            "UPDATE session_questions SET poll_id=?, message_id=?, open_ts=?, close_ts=? WHERE session_id=? AND q_no=?",
            (poll_id, msg.message_id, base.now_ts(), base.now_ts() + effective_seconds, session_id, next_index),
        )
        conn.execute(
            "UPDATE sessions SET current_index=?, active_poll_id=?, active_poll_message_id=? WHERE id=?",
            (next_index, poll_id, msg.message_id, session_id),
        )
        conn.commit()

    context.job_queue.run_once(
        base.close_poll_job,
        when=max(1, effective_seconds),
        data={"session_id": session_id, "q_no": next_index},
        name=f"close:{session_id}:{next_index}",
    )


base.begin_or_advance_exam = begin_or_advance_exam


async def send_private_results(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session:
        return
    chat_row = base.DBH.fetchone("SELECT username FROM known_chats WHERE chat_id=?", (session["chat_id"],))
    username = chat_row["username"] if chat_row else None
    ranking = base.get_session_ranking(session_id)
    rank_map = {int(r["user_id"]): r for r in ranking}
    total_users = max(1, len(ranking))
    qrows = base.DBH.fetchall("SELECT q_no, message_id FROM session_questions WHERE session_id=? ORDER BY q_no", (session_id,))
    q_map = {int(r["q_no"]): r for r in qrows}
    participants = base.DBH.fetchall("SELECT * FROM participants WHERE session_id=? AND eligible=1", (session_id,))
    total_questions = int(session["total_questions"] or 0)

    for p in participants:
        row = base.DBH.fetchone("SELECT started FROM known_users WHERE user_id=?", (p["user_id"],))
        if not row or int(row["started"] or 0) != 1:
            continue
        rank_item = rank_map.get(int(p["user_id"]))
        if not rank_item:
            continue
        if not await base.is_required_channel_member(context, int(p["user_id"])):
            continue
        answers = base.DBH.fetchall("SELECT * FROM answers WHERE session_id=? AND user_id=? ORDER BY q_no", (session_id, p["user_id"]))
        answer_by_q = {int(a["q_no"]): a for a in answers}
        correct_links: List[str] = []
        wrong_links: List[str] = []
        skipped_links: List[str] = []
        for q_no, q in q_map.items():
            link = base.get_message_link(int(session["chat_id"]), int(q["message_id"] or 0), username)
            label = f"<a href=\"{link}\">Q{q_no}</a>" if link else f"Q{q_no}"
            ans = answer_by_q.get(q_no)
            if ans is None:
                skipped_links.append(label)
            elif int(ans["is_correct"]) == 1:
                correct_links.append(label)
            else:
                wrong_links.append(label)
        correct = int(rank_item["correct"])
        wrong = int(rank_item["wrong"])
        attempted = max(1, correct + wrong)
        accuracy = (correct / attempted) * 100.0
        percentage = (correct / max(1, total_questions)) * 100.0
        if total_users <= 1:
            percentile = 100.0
        else:
            percentile = ((total_users - int(rank_item["rank"])) / (total_users - 1)) * 100.0
        text = (
            f"<b>{base.html_escape(session['title'])}</b>\n"
            f"Your rank: <b>#{rank_item['rank']}</b> / {total_users}\n"
            f"✅ Correct: <b>{correct}</b>\n"
            f"❌ Wrong: <b>{wrong}</b>\n"
            f"➖ Skipped: <b>{rank_item['skipped']}</b>\n"
            f"🏁 Final Score: <b>{rank_item['score']}</b>\n\n"
            f"Accuracy: <b>{accuracy:.2f}%</b>\n"
            f"Percentage: <b>{percentage:.2f}%</b>\n"
            f"Percentile: <b>{percentile:.2f}</b>\n\n"
            f"<b>Correct Links</b>\n{', '.join(correct_links) or '—'}\n\n"
            f"<b>Wrong Links</b>\n{', '.join(wrong_links) or '—'}\n\n"
            f"<b>Skipped Links</b>\n{', '.join(skipped_links) or '—'}"
        )
        with suppress(TelegramError):
            await context.bot.send_message(
                chat_id=int(p["user_id"]),
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


base.send_private_results = send_private_results


async def _stop_current_poll_and_jobs(context, session: Any) -> None:
    sid = str(session["id"])
    current_index = int(session["current_index"] or 0)
    close_job_name = f"close:{sid}:{current_index}"
    for job in list(context.job_queue.get_jobs_by_name(close_job_name)):
        job.schedule_removal()
    for job in list(context.job_queue.get_jobs_by_name(f"advance:{sid}")):
        job.schedule_removal()
    for job in list(context.job_queue.jobs()):
        if job.name and str(job.name).startswith(f"advance:{sid}:"):
            job.schedule_removal()
    if session["active_poll_message_id"]:
        with suppress(TelegramError):
            await context.bot.stop_poll(chat_id=session["chat_id"], message_id=int(session["active_poll_message_id"]))
    base.set_session_active_poll(sid, None, None)


async def handle_inline_query(update: Update, context) -> None:
    iq = update.inline_query
    if not iq or not iq.from_user:
        return
    user_id = iq.from_user.id
    if not base.user_has_staff_access(user_id):
        await iq.answer([], cache_time=0, is_personal=True)
        return
    query = base.normalize_visual_text(iq.query or "")
    drafts = base.list_user_drafts(user_id)
    filtered = []
    for row in drafts:
        q_count = int(row.get("q_count", 0) if isinstance(row, dict) else row["q_count"])
        if q_count <= 0:
            continue
        title = str(row["title"])
        code = str(row["id"])
        if not query:
            filtered.append(row)
            continue
        q_lower = query.casefold()
        if q_lower in code.casefold() or q_lower in title.casefold() or q_lower == f"quiz:{code.casefold()}":
            filtered.append(row)
    filtered = filtered[:20]
    bot_username = context.bot_data.get("bot_username", "")
    results: List[InlineQueryResultArticle] = []
    for row in filtered:
        practice = base.ensure_practice_link(str(row["id"]), int(row["owner_id"]))
        practice_url = f"https://t.me/{bot_username}?start=practice_{practice['token']}" if bot_username else ""
        text = (
            f"<b>{base.html_escape(row['title'])}</b>\n"
            f"Quiz ID: <code>{row['id']}</code>\n"
            f"Questions: <b>{row['q_count']}</b>\n"
            f"Time / question: <b>{row['question_time']} sec</b>\n"
            f"Negative / wrong: <b>{row['negative_mark']}</b>"
        )
        if practice_url:
            text += f"\n\nPractice link:\n{practice_url}"
        results.append(
            InlineQueryResultArticle(
                id=str(row["id"]),
                title=f"{row['title']} [{row['id']}]",
                description=f"Q: {row['q_count']} | {row['question_time']}s | -{row['negative_mark']}",
                input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML),
            )
        )
    await iq.answer(results, cache_time=0, is_personal=True)


_prev_build_app = base.build_app

def build_app() -> Application:
    app = _prev_build_app()
    app.add_handler(InlineQueryHandler(handle_inline_query), group=3)
    return app


base.build_app = build_app


def everyone_private_commands() -> List[BotCommand]:
    return [
        BotCommand("start", "Activate bot / open practice links"),
        BotCommand("help", "Help and commands"),
        BotCommand("commands", "Command list"),
        BotCommand("pauseq", "Pause your private practice"),
        BotCommand("resumeq", "Resume your private practice"),
        BotCommand("skipq", "Skip current private question"),
        BotCommand("stoptqex", "Stop active private exam or practice"),
    ]



def admin_private_commands() -> List[BotCommand]:
    return everyone_private_commands() + [
        BotCommand("panel", "Admin panel"),
        BotCommand("newexam", "Create new exam draft"),
        BotCommand("drafts", "My drafts"),
        BotCommand("csvformat", "CSV import format"),
        BotCommand("importtext", "Import MCQs from text / TXT"),
        BotCommand("txtquiz", "Alias of importtext"),
        BotCommand("clonequiz", "Start QuizBot clone workflow"),
        BotCommand("cloneend", "Finish clone workflow"),
        BotCommand("draftinfo", "Show draft details"),
        BotCommand("settitle", "Edit draft title"),
        BotCommand("settime", "Edit time per question"),
        BotCommand("setneg", "Edit negative marking"),
        BotCommand("shuffle", "Shuffle draft questions"),
        BotCommand("delq", "Delete question numbers"),
        BotCommand("section", "Add section timing"),
        BotCommand("sections", "List draft sections"),
        BotCommand("clearsections", "Remove all sections"),
        BotCommand("creator", "Show draft creator info"),
        BotCommand("renamefile", "Rename a file in bot inbox"),
        BotCommand("setthumb", "Set preview thumbnail"),
        BotCommand("clearthumb", "Clear thumbnail"),
        BotCommand("thumbstatus", "Thumbnail status"),
        BotCommand("cancel", "Cancel current input flow"),
    ]



def owner_private_commands() -> List[BotCommand]:
    return admin_private_commands() + [
        BotCommand("addadmin", "Add isolated admin"),
        BotCommand("addadminalp", "Add all-access admin"),
        BotCommand("rmadmin", "Remove admin"),
        BotCommand("admins", "List admin roles"),
        BotCommand("audit", "Recent admin actions"),
        BotCommand("logs", "Bot logs summary"),
        BotCommand("broadcast", "Broadcast to groups and users"),
        BotCommand("announce", "Announce to one chat"),
        BotCommand("restart", "Restart bot"),
    ]



def group_admin_commands() -> List[BotCommand]:
    return [
        BotCommand("binddraft", "Bind a draft to this group"),
        BotCommand("examstatus", "Show current draft and exam state"),
        BotCommand("starttqex", "Show ready button or start selected exam"),
        BotCommand("pauseq", "Pause after the current question"),
        BotCommand("resumeq", "Resume a paused exam"),
        BotCommand("skipq", "Skip the current question"),
        BotCommand("speed", "Change next-question speed"),
        BotCommand("stoptqex", "Stop the running exam"),
        BotCommand("schedule", "Schedule the active or bound draft"),
        BotCommand("listschedules", "List scheduled exams"),
        BotCommand("cancelschedule", "Cancel a schedule"),
    ]



def build_commands_text(chat_type: str, is_admin_user: bool, is_owner_user: bool) -> str:
    lines: List[str] = [
        "<b>Command List</b>",
        "All commands work with both <b>/</b> and <b>.</b> prefixes.",
        "",
    ]
    if chat_type == "private":
        lines.extend([
            "<b>Everyone</b>",
            "• /start — activate the bot / open practice links / receive DM results",
            "• /start practice_TOKEN — open a generated practice exam",
            "• /pauseq — pause your private practice after the current question",
            "• /resumeq — resume a paused private practice",
            "• /skipq — skip the current private question",
            "• /stoptqex — stop your current private practice or exam",
            "• /help or /commands — command list",
        ])
        if is_admin_user:
            lines.extend([
                "",
                "<b>Admin / Owner (Private)</b>",
                "• /panel — open the admin panel",
                "• /newexam — create a new exam draft",
                "• /drafts or /mydrafts — list drafts",
                "• /importtext or /txtquiz — import questions from pasted text or a TXT file",
                "• /clonequiz — create a new draft for forwarded @QuizBot quiz polls",
                "• /cloneend — finish the current clone workflow",
                "• /draftinfo [CODE] — show full draft details",
                "• /settitle CODE | New Title — change draft title",
                "• /settime CODE 30 — change default time per question",
                "• /setneg CODE 0.25 — change negative marking",
                "• /shuffle CODE — shuffle draft questions",
                "• /delq CODE 3,5-7 — delete question numbers",
                "• /section CODE 1-10 | Biology | 30 — add a timed section",
                "• /sections CODE — list draft sections",
                "• /clearsections CODE — remove all sections from a draft",
                "• /creator CODE — show quiz creator info",
                "• /csvformat — CSV import format",
                "• /renamefile — rename a file in bot inbox and resend it",
                "• /setthumb — set a custom preview thumbnail",
                "• /clearthumb — remove the custom thumbnail",
                "• /thumbstatus — show current thumbnail status",
                "• inline query: type <code>@YourBotName quiz:CODE</code> after enabling inline mode in BotFather",
                "• /cancel — cancel the current input flow",
            ])
        if is_owner_user:
            lines.extend([
                "",
                "<b>Owner Only</b>",
                "• /addadmin USER_ID — add an isolated admin",
                "• /addadminalp USER_ID — add an all-access admin",
                "• /rmadmin USER_ID — remove an admin",
                "• /admins — list admin roles",
                "• /audit — recent admin actions",
                "• /logs — memory, uptime, and recent errors",
                "• /broadcast [pin] — broadcast to groups and users",
                "• /announce CHAT_ID [pin] — announce to one chat",
                "• /restart — restart the bot process",
            ])
    else:
        lines.extend([
            "<b>Group Admin / Bot Admin</b>",
            "• /binddraft CODE — bind a draft to this group",
            "• /examstatus — show the current binding and exam status",
            "• /starttqex [DRAFTCODE] — show the ready button or start a selected exam",
            "• /pauseq — pause after the current question",
            "• /resumeq — resume a paused exam",
            "• /skipq — skip the current question",
            "• /speed slow|normal|fast — apply a new speed from the next question",
            "• /stoptqex — stop the running exam",
            "• /schedule YYYY-MM-DD HH:MM — schedule the active or bound draft",
            "• /listschedules — list scheduled exams for this group",
            "• /cancelschedule SCHEDULE_ID — cancel a schedule",
        ])
    return "\n".join(lines)


base.everyone_private_commands = everyone_private_commands
base.admin_private_commands = admin_private_commands
base.owner_private_commands = owner_private_commands
base.group_admin_commands = group_admin_commands
base.build_commands_text = build_commands_text


_prev_handle_document_upload = base.handle_document_upload


async def handle_document_upload(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message and user and chat and message.document and chat.type == "private" and base.user_has_staff_access(user.id):
        state, payload = base.get_user_state(user.id)
        lower_name = (message.document.file_name or "").lower()
        if state == "adv_await_import_text" and lower_name.endswith((".txt", ".md", ".json")):
            file = await message.document.get_file()
            data = bytes(await file.download_as_bytearray())
            clear_text = data.decode("utf-8-sig", errors="replace")
            draft_id = str(payload.get("draft_id") or "")
            base.clear_user_state(user.id)
            if not draft_id:
                await base.safe_reply(message, "No draft is selected for text import.")
                return
            await import_text_into_draft(message, context, draft_id, clear_text, src=f"txt:{message.document.file_name or 'upload.txt'}")
            return
    return await _prev_handle_document_upload(update, context)


base.handle_document_upload = handle_document_upload


_prev_handle_poll_import = base.handle_poll_import


async def handle_poll_import(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.poll:
        return await _prev_handle_poll_import(update, context)
    if chat.type == "private" and base.is_bot_admin(user.id):
        clone = get_clone_session(user.id)
        draft_id = str(clone["draft_id"]) if clone else (base.get_active_draft_id(user.id) or "")
        if draft_id and message.poll.type == Poll.QUIZ and message.poll.correct_option_id is not None:
            cleaned_question = clean_forwarded_text(message.poll.question)
            cleaned_options = [clean_forwarded_text(opt.text) for opt in message.poll.options]
            cleaned_expl = clean_forwarded_text(message.poll.explanation or "")
            ok, q_no = dedup_add_question_to_draft(
                draft_id,
                cleaned_question,
                cleaned_options,
                int(message.poll.correct_option_id),
                cleaned_expl,
                "quizbot_clone" if clone else "forwarded_quiz",
            )
            if ok:
                header = f"✅ {'Clone' if clone else 'Draft'} updated. Added question Q{q_no}"
            else:
                header = "ℹ️ Duplicate question skipped."
            await base.send_draft_card(context, user.id, user.id, draft_id, header=header)
            base.audit(user.id, "clone_import" if clone else "add_quiz_question", draft_id, {"added": bool(ok), "q_no": q_no})
            return
    return await _prev_handle_poll_import(update, context)


base.handle_poll_import = handle_poll_import


_prev_handle_text = base.handle_text


async def handle_text(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not getattr(message, "text", None):
        return await _prev_handle_text(update, context)

    state, payload = base.get_user_state(user.id)
    cmd, args = base.extract_command(message.text, context.bot_data.get("bot_username", ""))
    cmd = (cmd or "").lower()

    if chat.type == "private" and state == "adv_await_import_text" and not cmd:
        draft_id = str(payload.get("draft_id") or "")
        base.clear_user_state(user.id)
        if not draft_id:
            await base.safe_reply(message, "No draft is selected for this text import.")
            return
        await import_text_into_draft(message, context, draft_id, message.text, src="pasted_text")
        return

    if chat.type == "private" and state == "adv_await_clone_source" and not cmd:
        token = extract_clone_token(message.text)
        if not token:
            await base.safe_reply(message, "Send a valid @QuizBot inline text like <code>@QuizBot quiz:ABCDE</code> or a message that contains <code>quiz:ABCDE</code>.", parse_mode=ParseMode.HTML)
            return
        title = str(payload.get("title") or f"QuizBot Clone {token}")
        draft_id = base.create_draft(user.id, title, 30, 0.0)
        start_clone_session(user.id, draft_id, token, message.text)
        base.clear_user_state(user.id)
        await base.send_draft_card(
            context,
            user.id,
            user.id,
            draft_id,
            header=(
                "✅ Clone draft created.\n"
                "Now forward the quiz polls from @QuizBot to this bot inbox. Each forwarded quiz poll will be cleaned and added automatically.\n"
                "Use /cloneend when finished."
            ),
        )
        return

    if chat.type == "private" and base.user_has_staff_access(user.id):
        if cmd in {"importtext", "txtquiz"}:
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, "Select an active draft first, or pass the draft code: /importtext DRAFTCODE")
                return
            base.set_user_state(user.id, "adv_await_import_text", {"draft_id": draft["id"]})
            await base.safe_reply(
                message,
                "Send the MCQ text now, or upload a .txt/.md/.json file.\n\nSupported format example:\n\n1. What is the capital of France?\nA. Berlin\nB. Madrid\nC. Paris ✅\nD. Rome\nExplanation: Paris is the capital.",
            )
            return

        if cmd == "clonequiz":
            raw = args.strip()
            if raw:
                if "|" in raw:
                    title_part, source_part = [x.strip() for x in raw.split("|", 1)]
                else:
                    title_part, source_part = "", raw
                token = extract_clone_token(source_part)
                if token:
                    draft_id = base.create_draft(user.id, title_part or f"QuizBot Clone {token}", 30, 0.0)
                    start_clone_session(user.id, draft_id, token, source_part)
                    await base.send_draft_card(
                        context,
                        user.id,
                        user.id,
                        draft_id,
                        header=(
                            "✅ Clone draft created.\n"
                            "Forward the quiz polls from @QuizBot to this bot inbox. Each forwarded quiz poll will be cleaned and added automatically.\n"
                            "Use /cloneend when finished."
                        ),
                    )
                    return
            base.set_user_state(user.id, "adv_await_clone_source", {"title": ""})
            await base.safe_reply(
                message,
                "Send the @QuizBot inline text or any message that contains <code>quiz:YOUR_ID</code>.\n\nNote: Telegram Bot API cannot directly fetch another bot's inline quiz payload by only reading the pasted token. This build uses a guided clone workflow: it creates a draft, then imports the forwarded quiz polls automatically.",
                parse_mode=ParseMode.HTML,
            )
            return

        if cmd == "cloneend":
            clone = get_clone_session(user.id)
            if not clone:
                await base.safe_reply(message, "There is no active clone session.")
                return
            stop_clone_session(user.id)
            await base.send_draft_card(context, user.id, user.id, clone["draft_id"], header="✅ Clone session finished.")
            return

        if cmd == "draftinfo":
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            await base.safe_reply(message, format_draft_info(draft), parse_mode=ParseMode.HTML)
            return

        if cmd == "creator":
            code = base.normalize_visual_text(args).upper()
            if not code:
                await base.safe_reply(message, "Usage: /creator DRAFTCODE")
                return
            draft = base.get_draft(code)
            if not draft:
                await base.safe_reply(message, "Draft not found.")
                return
            q_count_row = base.DBH.fetchone("SELECT COUNT(*) AS c FROM draft_questions WHERE draft_id=?", (code,))
            role = "owner" if base.is_owner(int(draft["owner_id"])) else ("all-access admin" if getattr(base, "is_all_access_admin", lambda _x: False)(int(draft["owner_id"])) else "admin")
            text = (
                f"<b>Creator Info</b>\n"
                f"Draft: <b>{base.html_escape(draft['title'])}</b>\n"
                f"Code: <code>{draft['id']}</code>\n"
                f"Creator ID: <code>{draft['owner_id']}</code>\n"
                f"Role: <b>{role}</b>\n"
                f"Questions: <b>{int(q_count_row['c'] if q_count_row else 0)}</b>\n"
                f"Created: <b>{base.fmt_dt(draft['created_at'])}</b>\n"
                f"Updated: <b>{base.fmt_dt(draft['updated_at'])}</b>"
            )
            await base.safe_reply(message, text, parse_mode=ParseMode.HTML)
            return

        if cmd == "settitle":
            if "|" not in args:
                await base.safe_reply(message, "Usage: /settitle DRAFTCODE | New Title")
                return
            code_part, title_part = [x.strip() for x in args.split("|", 1)]
            draft = resolve_editable_draft(user.id, code_part)
            if not draft or not title_part:
                await base.safe_reply(message, "Draft not found or title is empty.")
                return
            base.DBH.execute("UPDATE drafts SET title=?, updated_at=? WHERE id=?", (base.normalize_visual_text(title_part), base.now_ts(), draft["id"]))
            await base.send_draft_card(context, user.id, user.id, draft["id"], header="✅ Draft title updated.")
            return

        if cmd == "settime":
            parts = args.split()
            if len(parts) < 2 or not parts[-1].isdigit():
                await base.safe_reply(message, "Usage: /settime DRAFTCODE 30")
                return
            draft = resolve_editable_draft(user.id, " ".join(parts[:-1]))
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            secs = max(5, int(parts[-1]))
            base.DBH.execute("UPDATE drafts SET question_time=?, updated_at=? WHERE id=?", (secs, base.now_ts(), draft["id"]))
            await base.send_draft_card(context, user.id, user.id, draft["id"], header=f"✅ Default time updated to {secs} sec.")
            return

        if cmd == "setneg":
            parts = args.split()
            if len(parts) < 2:
                await base.safe_reply(message, "Usage: /setneg DRAFTCODE 0.25")
                return
            try:
                neg = float(parts[-1])
            except ValueError:
                await base.safe_reply(message, "Send a valid decimal value. Example: 0.25")
                return
            draft = resolve_editable_draft(user.id, " ".join(parts[:-1]))
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            base.DBH.execute("UPDATE drafts SET negative_mark=?, updated_at=? WHERE id=?", (neg, base.now_ts(), draft["id"]))
            await base.send_draft_card(context, user.id, user.id, draft["id"], header=f"✅ Negative mark updated to {neg}.")
            return

        if cmd == "shuffle":
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            shuffle_draft_questions(draft["id"])
            await base.send_draft_card(context, user.id, user.id, draft["id"], header="✅ Draft questions shuffled.")
            return

        if cmd == "delq":
            parts = args.split(maxsplit=1)
            if len(parts) != 2:
                await base.safe_reply(message, "Usage: /delq DRAFTCODE 3,5-7")
                return
            draft = resolve_editable_draft(user.id, parts[0])
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            numbers = parse_q_number_list(parts[1])
            removed = delete_question_numbers(draft["id"], numbers)
            await base.send_draft_card(context, user.id, user.id, draft["id"], header=f"✅ Removed {removed} question(s).")
            return

        if cmd == "section":
            if "|" not in args:
                await base.safe_reply(message, "Usage: /section DRAFTCODE 1-10 | Biology | 30")
                return
            left, title, time_part = [x.strip() for x in args.split("|", 2)] if args.count("|") >= 2 else [x.strip() for x in args.split("|", 1)] + [""]
            bits = left.split()
            if len(bits) < 2:
                await base.safe_reply(message, "Usage: /section DRAFTCODE 1-10 | Biology | 30")
                return
            draft = resolve_editable_draft(user.id, bits[0])
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            rng = bits[1]
            if "-" not in rng:
                await base.safe_reply(message, "Use a question range like 1-10.")
                return
            a, b = rng.split("-", 1)
            if not (a.strip().isdigit() and b.strip().isdigit()):
                await base.safe_reply(message, "Use numeric question ranges like 1-10.")
                return
            q_time = int(time_part) if time_part.strip().isdigit() else None
            set_section(draft["id"], int(a), int(b), title or f"Section {a}-{b}", q_time)
            await base.safe_reply(message, f"✅ Section added to <code>{draft['id']}</code>.", parse_mode=ParseMode.HTML)
            return

        if cmd == "sections":
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            rows = list_sections(draft["id"])
            if not rows:
                await base.safe_reply(message, "No sections are configured for this draft.")
                return
            lines = [f"<b>Sections for {base.html_escape(draft['title'])}</b>"]
            for row in rows:
                lines.append(
                    f"• {base.html_escape(row['title'])} — Q{row['start_q']}-Q{row['end_q']}" + (f" — {row['question_time']} sec" if row['question_time'] else "")
                )
            await base.safe_reply(message, "\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if cmd == "clearsections":
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, "Draft not found, or you do not have access.")
                return
            clear_sections(draft["id"])
            await base.safe_reply(message, f"✅ All sections removed from <code>{draft['id']}</code>.", parse_mode=ParseMode.HTML)
            return

    # Everyone can control their own private practice.
    if chat.type == "private" and cmd in {"pauseq", "resumeq", "skipq"}:
        session = base.get_active_session(user.id)
        if cmd == "resumeq":
            paused = base.DBH.fetchone("SELECT * FROM sessions WHERE chat_id=? AND status='paused' ORDER BY started_at DESC LIMIT 1", (user.id,))
            if not paused:
                await base.safe_reply(message, "There is no paused private practice.")
                return
            base.DBH.execute("UPDATE sessions SET status='running', paused_at=NULL WHERE id=?", (paused["id"],))
            context.job_queue.run_once(base.begin_or_advance_exam_job, when=0.4, data={"session_id": paused["id"]}, name=f"advance:{paused['id']}:resume")
            await base.safe_reply(message, "▶️ Private practice resumed.")
            return
        if not session:
            await base.safe_reply(message, "There is no active private practice right now.")
            return
        if cmd == "pauseq":
            await _stop_current_poll_and_jobs(context, session)
            base.DBH.execute("UPDATE sessions SET status='paused', paused_at=? WHERE id=?", (base.now_ts(), session["id"]))
            await base.safe_reply(message, "⏸ Private practice paused. Use /resumeq to continue.")
            return
        if cmd == "skipq":
            await _stop_current_poll_and_jobs(context, session)
            context.job_queue.run_once(base.begin_or_advance_exam_job, when=0.4, data={"session_id": session["id"]}, name=f"advance:{session['id']}:skip")
            await base.safe_reply(message, "⏭ Current question skipped.")
            return

    if chat.type in {"group", "supergroup"} and cmd in {"pauseq", "resumeq", "skipq", "speed"}:
        if not await base.is_group_admin_or_global(update, context):
            return await base.handle_group_denied_command(update, context)
        if cmd == "resumeq":
            paused = base.DBH.fetchone("SELECT * FROM sessions WHERE chat_id=? AND status='paused' ORDER BY started_at DESC LIMIT 1", (chat.id,))
            if not paused:
                await base.safe_reply(message, "There is no paused exam in this group.")
                return
            base.DBH.execute("UPDATE sessions SET status='running', paused_at=NULL WHERE id=?", (paused["id"],))
            context.job_queue.run_once(base.begin_or_advance_exam_job, when=0.6, data={"session_id": paused["id"]}, name=f"advance:{paused['id']}:resume")
            await base.safe_reply(message, "▶️ Exam resumed. The next question is coming now.")
            return

        session = base.get_active_session(chat.id)
        if not session:
            await base.safe_reply(message, "There is no active exam in this group.")
            return

        if cmd == "pauseq":
            await _stop_current_poll_and_jobs(context, session)
            base.DBH.execute("UPDATE sessions SET status='paused', paused_at=? WHERE id=?", (base.now_ts(), session["id"]))
            await base.safe_reply(message, "⏸ Exam paused. Use /resumeq to continue.")
            return

        if cmd == "skipq":
            await _stop_current_poll_and_jobs(context, session)
            context.job_queue.run_once(base.begin_or_advance_exam_job, when=0.6, data={"session_id": session["id"]}, name=f"advance:{session['id']}:skip")
            await base.safe_reply(message, "⏭ Current question skipped.")
            return

        if cmd == "speed":
            mode = base.normalize_visual_text(args).lower()
            if mode not in SPEED_PRESETS:
                await base.safe_reply(message, "Usage: /speed slow|normal|fast")
                return
            factor, mode_name = SPEED_PRESETS[mode]
            base.DBH.execute("UPDATE sessions SET speed_factor=?, speed_mode=? WHERE id=?", (factor, mode_name, session["id"]))
            await base.safe_reply(message, f"⚙️ Speed set to <b>{mode_name}</b>. It will apply from the next question.", parse_mode=ParseMode.HTML)
            return

    return await _prev_handle_text(update, context)


base.handle_text = handle_text


_prev_send_admin_pdf_report = base.send_admin_pdf_report


async def send_admin_pdf_report(context, session_id: str, ranking: List[Dict[str, Any]]) -> None:
    session = base.get_session(session_id)
    if not session:
        return
    rows = base.DBH.fetchall("SELECT score FROM participants WHERE session_id=? AND eligible=1", (session_id,))
    scores = [float(r["score"]) for r in rows] or [0.0]
    creator_id = int(session["created_by"])
    if hasattr(base, 'get_user_theme'):
        _name, theme, _custom = base.get_user_theme(creator_id)
    else:
        theme = getattr(base, 'BUILTIN_THEMES', {'midnight': {'bg':'#03101F','text':'#EAF2FF','muted':'#B9C7DD','table':'#07162D','card1':'#132744','card2':'#0E2037','subtext':'#C8D8F4','accent':'#D7F7CC','footer':'#95A0B4','outline':'#18324B'}})['midnight']
    summary = {
        "participants": len(ranking),
        "questions": int(session["total_questions"]),
        "average_score": base.fmt_score(sum(scores) / len(scores)),
        "highest_score": base.fmt_score(max(scores)),
        "lowest_score": base.fmt_score(min(scores)),
        "negative_mark": session["negative_mark"],
        "started_at": base.fmt_dt(session["started_at"]),
        "ended_at": base.fmt_dt(session["ended_at"]),
    }
    compact = []
    for r in ranking:
        name = r["name"]
        if r.get("sub_name"):
            name = f"{name} {r['sub_name']}"
        compact.append({**r, "name": name, "sub_name": "", "time": r.get("time_label", "0s")})
    pdf_bytes = await asyncio.to_thread(base.render_report_pdf, session["title"], summary, compact, theme)
    html_doc = base._report_html(base.normalize_visual_text(session["title"]), summary, compact, theme) if hasattr(base, '_report_html') else None
    thumb_bytes = base.get_report_thumbnail_bytes(creator_id, session["title"]) if hasattr(base, 'get_report_thumbnail_bytes') else None
    recipients: List[int] = []
    for uid in [creator_id] + list(base.CONFIG.owner_ids) + base.all_admin_ids():
        if uid not in recipients:
            recipients.append(uid)
    for uid in recipients:
        try:
            kwargs = {}
            if thumb_bytes:
                kwargs['thumbnail'] = InputFile(base.io.BytesIO(thumb_bytes), filename='report_preview.jpg')
            await context.bot.send_document(
                uid,
                document=InputFile(base.io.BytesIO(pdf_bytes), filename=f"{base.pdf_safe_filename(session['title'])}_report.pdf"),
                caption=f"📄 {base.normalize_visual_text(session['title'])} analysis report",
                **kwargs,
            )
            if html_doc:
                await context.bot.send_document(
                    uid,
                    document=InputFile(base.io.BytesIO(html_doc.encode('utf-8')), filename=f"{base.pdf_safe_filename(session['title'])}_report.html"),
                    caption='HTML report (light/dark capable in browser).',
                )
        except TelegramError as exc:
            base.logger.warning("Could not send report files to %s: %s", uid, exc)


base.send_admin_pdf_report = send_admin_pdf_report

# ============================================================
# Final UX patch v4: clean inline draft editor, prefix toggle,
# robust import sanitization, HTML export, website-style result report
# ============================================================

ensure_column("drafts", "show_title_prefix", "INTEGER DEFAULT 1")
base.DBH.execute("UPDATE drafts SET show_title_prefix=1 WHERE show_title_prefix IS NULL")
ensure_column("drafts", "html_export_theme", "TEXT DEFAULT 'auto'")

_ADV_EDIT_STATES = {
    "adv2_edit_title",
    "adv2_edit_time",
    "adv2_edit_neg",
    "adv2_add_questions",
    "adv2_del_questions",
    "adv2_add_section",
}


def _smart_clean_question_text(raw: str) -> str:
    original = base.normalize_visual_text(urllib.parse.unquote(raw or ""))
    if not original:
        return ""
    value = original
    value = re.sub(r"/view_[A-Za-z0-9_]+", " ", value)
    value = COUNTER_RE.sub("", value)
    value = re.sub(r"\[[^\]]{0,120}?@[^\]]+\]", " ", value)
    value = re.sub(r"\bvia\b\s+@?[A-Za-z0-9_]+", " ", value, flags=re.I)
    value = URL_RE.sub(" ", value)
    value = USERNAME_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" -–—|•[]")
    if value:
        return value
    fallback = re.sub(r"/view_[A-Za-z0-9_]+", " ", original)
    fallback = COUNTER_RE.sub("", fallback)
    fallback = re.sub(r"\s+", " ", fallback).strip(" -–—|•[]")
    return fallback


def _smart_clean_option_text(raw: str) -> str:
    value = base.normalize_visual_text(urllib.parse.unquote(raw or ""))
    if not value:
        return ""
    for mark in CHECKMARKS:
        value = value.replace(mark, "")
    value = value.replace("*", "")
    value = re.sub(r"/view_[A-Za-z0-9_]+", " ", value)
    value = URL_RE.sub(" ", value)
    value = USERNAME_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" -–—|•[]")


def _smart_clean_explanation_text(raw: str) -> str:
    value = base.normalize_visual_text(urllib.parse.unquote(raw or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_forwarded_text(text: str) -> str:
    return _smart_clean_question_text(text)


def _extract_option_text_and_mark(line: str) -> Tuple[Optional[str], bool]:
    stripped = base.normalize_visual_text(line or "")
    if not stripped:
        return None, False
    marked = any(mark in stripped for mark in CHECKMARKS) or stripped.rstrip().endswith("*")
    m = re.match(r"^\s*(?:\(([A-Ja-j])\)|([A-Ja-j])[\).]|[-*•])\s*(.+?)\s*$", stripped)
    if not m:
        return None, marked
    text = _smart_clean_option_text(m.group(3) or "")
    return (text or None), marked


def _looks_like_question_start(line: str) -> bool:
    stripped = base.normalize_visual_text(line or "")
    if not stripped:
        return False
    return bool(re.match(r"^\s*\d+[\).]\s*", stripped))


def _looks_like_explanation(line: str) -> bool:
    stripped = base.normalize_visual_text(line or "")
    return bool(re.match(r"^\s*(?:ব্যাখ্যা|explanation|explain|reason|note)\s*[:：-]", stripped, flags=re.I))


_prev_parse_marked_questions = parse_marked_questions_from_text

def parse_marked_questions_from_text(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").replace("\r", "")
    if not raw.strip():
        return []

    parsed = _prev_parse_marked_questions(raw)
    if parsed:
        return parsed

    lines: List[str] = []
    for line in raw.split("\n"):
        stripped = base.normalize_visual_text(line)
        if stripped.lower() == "n":
            lines.append("")
        else:
            lines.append(stripped)

    blocks: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(current)
                current = []
            continue
        if _looks_like_question_start(stripped) and current:
            blocks.append(current)
            current = [stripped]
        else:
            current.append(stripped)
    if current:
        blocks.append(current)

    out: List[Dict[str, Any]] = []
    for block in blocks:
        question_parts: List[str] = []
        options: List[str] = []
        correct_idx: Optional[int] = None
        answer_ref: Optional[str] = None
        explanation_parts: List[str] = []
        current_option = -1
        saw_option = False

        for idx, line in enumerate(block):
            line_norm = base.normalize_visual_text(line)
            ans_m = ANSWER_RE.match(line_norm)
            if ans_m:
                answer_ref = ans_m.group(1).strip()
                continue
            if _looks_like_explanation(line_norm):
                expl = re.sub(r"^\s*(?:ব্যাখ্যা|explanation|explain|reason|note)\s*[:：-]\s*", "", line_norm, flags=re.I)
                if expl:
                    explanation_parts.append(expl)
                continue
            opt_text, marked = _extract_option_text_and_mark(line_norm)
            if opt_text is not None:
                saw_option = True
                options.append(opt_text)
                current_option = len(options) - 1
                if marked:
                    correct_idx = current_option
                continue
            if saw_option and current_option >= 0:
                options[current_option] = base.normalize_visual_text(f"{options[current_option]} {line_norm}")
                continue
            q_line = QUESTION_PREFIX_RE.sub("", line_norm) if idx == 0 else line_norm
            if q_line:
                question_parts.append(q_line)

        question = _smart_clean_question_text(" ".join(question_parts))
        cleaned_options = [_smart_clean_option_text(x) for x in options if _smart_clean_option_text(x)]
        if correct_idx is None and answer_ref is not None:
            correct_idx = parse_answer_ref(answer_ref, cleaned_options)
        if question and len(cleaned_options) >= 2 and correct_idx is not None and 0 <= int(correct_idx) < len(cleaned_options):
            out.append({
                "question": question,
                "options": cleaned_options,
                "correct_option": int(correct_idx),
                "explanation": _smart_clean_explanation_text(" ".join(explanation_parts)),
            })
    return out


def _clean_and_map_options(raw_options: Iterable[Any], correct_option: int) -> Tuple[List[str], Optional[int]]:
    cleaned: List[str] = []
    index_map: Dict[int, int] = {}
    for idx, opt in enumerate(list(raw_options or [])):
        text = _smart_clean_option_text(str(opt))
        if not text:
            continue
        index_map[idx] = len(cleaned)
        cleaned.append(text)
    if correct_option not in index_map:
        return cleaned, None
    return cleaned, index_map[correct_option]


_prev_dedup_add = dedup_add_question_to_draft

def dedup_add_question_to_draft(draft_id: str, question: str, options: List[str], correct_option: int, explanation: str, src: str) -> Tuple[bool, Optional[int]]:
    q = _smart_clean_question_text(question)
    cleaned_options, new_correct = _clean_and_map_options(options, int(correct_option))
    exp = _smart_clean_explanation_text(explanation)
    if not q or len(cleaned_options) < 2 or new_correct is None:
        return False, None
    sig = question_signature(q, cleaned_options)
    if sig in existing_question_signatures(draft_id):
        return False, None
    q_no = base.add_question_to_draft(draft_id, q, cleaned_options, int(new_correct), exp, src)
    return True, q_no


def sanitize_existing_draft_questions(draft_id: str) -> Dict[str, int]:
    rows = base.get_draft_questions(draft_id)
    rebuilt: List[Dict[str, Any]] = []
    removed = 0
    seen: set[str] = set()
    for row in rows:
        q = _smart_clean_question_text(str(row["question"] or ""))
        raw_opts = base.jload(row["options"], []) or []
        clean_opts, new_correct = _clean_and_map_options(raw_opts, int(row["correct_option"]))
        exp = _smart_clean_explanation_text(str(row["explanation"] or ""))
        if not q or len(clean_opts) < 2 or new_correct is None:
            removed += 1
            continue
        sig = question_signature(q, clean_opts)
        if sig in seen:
            removed += 1
            continue
        seen.add(sig)
        rebuilt.append({
            "question": q,
            "options": clean_opts,
            "correct_option": int(new_correct),
            "explanation": exp,
            "src": str(row["src"] or "sanitized"),
        })

    if removed or len(rebuilt) != len(rows):
        with closing(base.DBH.connect()) as conn:
            conn.execute("DELETE FROM draft_questions WHERE draft_id=?", (draft_id,))
            for idx, item in enumerate(rebuilt, start=1):
                conn.execute(
                    "INSERT INTO draft_questions(draft_id, q_no, question, options, correct_option, explanation, src) VALUES(?,?,?,?,?,?,?)",
                    (draft_id, idx, item["question"], base.jdump(item["options"]), item["correct_option"], item["explanation"], item["src"]),
                )
            conn.commit()
        base.refresh_draft_status(draft_id)
    return {"kept": len(rebuilt), "removed": removed}


def _calc_total_draft_questions(draft_id: str) -> int:
    row = base.DBH.fetchone("SELECT COUNT(*) AS c FROM draft_questions WHERE draft_id=?", (draft_id,))
    return int(row["c"] if row else 0)


def _current_creator_theme(owner_id: int) -> Dict[str, str]:
    if hasattr(base, 'get_user_theme'):
        _name, theme, _custom = base.get_user_theme(owner_id)
        return dict(theme)
    return dict(getattr(base, 'BUILTIN_THEMES', {'midnight': {'bg':'#03101F','text':'#EAF2FF','muted':'#B9C7DD','table':'#07162D','card1':'#132744','card2':'#0E2037','subtext':'#C8D8F4','accent':'#D7F7CC','footer':'#95A0B4','outline':'#18324B'}})['midnight'])


def _build_practice_url_v4(bot_username: str, draft_id: str, owner_id: int) -> Optional[str]:
    if not bot_username:
        return None
    practice = base.ensure_practice_link(draft_id, owner_id)
    return f"https://t.me/{bot_username}?start=practice_{practice['token']}"


def _section_summary_for_draft(draft_id: str) -> List[str]:
    rows = list_sections(draft_id)
    if not rows:
        return ["No sections configured."]
    lines: List[str] = []
    for row in rows[:12]:
        tail = f" — {row['question_time']} sec" if row['question_time'] else ""
        lines.append(f"• {base.html_escape(row['title'])} — Q{row['start_q']}-Q{row['end_q']}{tail}")
    return lines


def _draft_prefix_state(draft: Any) -> bool:
    try:
        return bool(int(draft['show_title_prefix'] or 1))
    except Exception:
        return True


def _build_draft_detail_text_markup(user_id: int, draft_id: str, page: int = 0, header: str = "", bot_username: str = "") -> Tuple[str, InlineKeyboardMarkup]:
    draft = base.get_draft(draft_id)
    if not draft:
        return _build_draft_browser_list_text_markup(user_id, page=page, header="⚠️ Draft not found.")
    if int(draft['owner_id']) != user_id and not getattr(base, 'is_all_access_admin', lambda _x: False)(user_id):
        return _build_draft_browser_list_text_markup(user_id, page=page, header="⚠️ You do not have access to this draft.")
    sanitize_existing_draft_questions(draft_id)
    draft = base.get_draft(draft_id)
    q_count = _calc_total_draft_questions(draft_id)
    title_prefix = 'ON' if _draft_prefix_state(draft) else 'OFF'
    lines: List[str] = []
    if header:
        lines.append(header)
        lines.append("")
    lines.extend([
        "<b>Draft Details</b>",
        f"Title: <b>{base.html_escape(draft['title'])}</b>",
        f"Code: <code>{draft['id']}</code>",
        f"Questions: <b>{q_count}</b>",
        f"Time / question: <b>{draft['question_time']} sec</b>",
        f"Negative / wrong: <b>{draft['negative_mark']}</b>",
        f"Title prefix in poll: <b>{title_prefix}</b>",
        f"Created: <b>{base.fmt_dt(draft['created_at'])}</b>",
        f"Updated: <b>{base.fmt_dt(draft['updated_at'])}</b>",
        "",
        "<b>Sections</b>",
        *_section_summary_for_draft(draft_id),
    ])
    practice_url = _build_practice_url_v4(bot_username, draft_id, int(draft['owner_id'])) if q_count > 0 else None
    kb_rows: List[List[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton('📂 Open Draft', callback_data=f'ux:open:{draft_id}:{page}'),
            InlineKeyboardButton('🔄 Set Active', callback_data=f'ux:set:{draft_id}:{page}'),
        ],
        [
            InlineKeyboardButton(('⚙️ Prefix OFF' if _draft_prefix_state(draft) else '⚙️ Prefix ON'), callback_data=f'ux:prefix:{draft_id}:{page}'),
            InlineKeyboardButton('🎲 Shuffle', callback_data=f'ux:shuffle:{draft_id}:{page}'),
        ],
        [
            InlineKeyboardButton('✏️ Edit Title', callback_data=f'ux:ptitle:{draft_id}:{page}'),
            InlineKeyboardButton('⏱ Edit Time', callback_data=f'ux:ptime:{draft_id}:{page}'),
        ],
        [
            InlineKeyboardButton('➖ Edit Negative', callback_data=f'ux:pneg:{draft_id}:{page}'),
            InlineKeyboardButton('➕ Add Questions', callback_data=f'ux:padd:{draft_id}:{page}'),
        ],
        [
            InlineKeyboardButton('🗑 Delete Q', callback_data=f'ux:pdelq:{draft_id}:{page}'),
            InlineKeyboardButton('📚 Sections', callback_data=f'ux:psection:{draft_id}:{page}'),
        ],
        [InlineKeyboardButton('🌐 HTML Export', callback_data=f'ux:html:{draft_id}:{page}')],
    ]
    if practice_url:
        kb_rows.append([InlineKeyboardButton('🧪 Practice Link', url=practice_url)])
    kb_rows.append([
        InlineKeyboardButton('🗑 Delete', callback_data=f'ux:del:{draft_id}:{page}'),
        InlineKeyboardButton('📚 Draft Browser', callback_data=f'ux:browse:{page}'),
    ])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(kb_rows)


DRAFTS_PAGE_SIZE_V4 = 5

def _clamp_draft_page_v4(page: int, total: int) -> int:
    if total <= 0:
        return 0
    pages = max(1, (total + DRAFTS_PAGE_SIZE_V4 - 1) // DRAFTS_PAGE_SIZE_V4)
    return max(0, min(int(page), pages - 1))


def _build_draft_browser_list_text_markup(user_id: int, page: int = 0, header: str = "") -> Tuple[str, InlineKeyboardMarkup]:
    drafts = list(base.list_user_drafts(user_id))
    if getattr(base, 'is_all_access_admin', lambda _x: False)(user_id):
        extra = base.DBH.fetchall("SELECT d.*, COUNT(q.id) AS q_count FROM drafts d LEFT JOIN draft_questions q ON q.draft_id=d.id GROUP BY d.id ORDER BY d.updated_at DESC")
        seen = {str(r['id']) for r in drafts}
        for row in extra:
            if str(row['id']) not in seen:
                drafts.append(row)
                seen.add(str(row['id']))
    drafts = sorted(drafts, key=lambda r: int(r['updated_at']), reverse=True)
    if not drafts:
        text = ((header + "\n\n") if header else "") + "<b>Your Draft Browser</b>\n\nYou do not have any drafts yet."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Back', callback_data='panel:home')]])
        return text, kb
    page = _clamp_draft_page_v4(page, len(drafts))
    start = page * DRAFTS_PAGE_SIZE_V4
    end = min(start + DRAFTS_PAGE_SIZE_V4, len(drafts))
    page_rows = drafts[start:end]
    total_pages = max(1, (len(drafts) + DRAFTS_PAGE_SIZE_V4 - 1) // DRAFTS_PAGE_SIZE_V4)
    active_id = base.get_active_draft_id(user_id)
    lines: List[str] = []
    if header:
        lines.append(header)
        lines.append("")
    lines.append("<b>Your Draft Browser</b>")
    lines.append(f"Page <b>{page + 1}/{total_pages}</b> • Showing <b>{start + 1}-{end}</b> of <b>{len(drafts)}</b>")
    lines.append("")
    kb_rows: List[List[InlineKeyboardButton]] = []
    for idx, row in enumerate(page_rows, start=start + 1):
        is_active = active_id == row['id']
        prefix = 'ON' if int(row['show_title_prefix'] or 1) else 'OFF'
        lines.append(f"<b>{idx}. {base.html_escape(row['title'])}</b>")
        lines.append(f"Code: <code>{row['id']}</code>")
        lines.append(f"Questions: <b>{row['q_count']}</b>    Time: <b>{row['question_time']} sec</b>    Negative: <b>{row['negative_mark']}</b>")
        lines.append(f"Prefix: <b>{prefix}</b>    Status: <b>{'ACTIVE' if is_active else ('Ready' if int(row['q_count'] or 0) > 0 else 'Draft')}</b>")
        lines.append("")
        kb_rows.append([
            InlineKeyboardButton(f"📂 Open {row['id']}", callback_data=f"ux:open:{row['id']}:{page}"),
            InlineKeyboardButton(("✅ Active" if is_active else "🔄 Active"), callback_data=f"ux:set:{row['id']}:{page}"),
        ])
    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton('⬅️ Previous', callback_data=f'ux:browse:{page - 1}'))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton('➡️ Next', callback_data=f'ux:browse:{page + 1}'))
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([InlineKeyboardButton('⬅️ Back', callback_data='panel:home')])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(kb_rows)


async def _show_draft_browser(context: ContextTypes.DEFAULT_TYPE, user_id: int, page: int = 0, header: str = "") -> None:
    text, kb = _build_draft_browser_list_text_markup(user_id, page, header)
    if hasattr(base, '_drop_home_panel_if_present'):
        await base._drop_home_panel_if_present(context, user_id)
    if hasattr(base, '_replace_single_panel_message'):
        await base._replace_single_panel_message(context, user_id, ('ux-browser', user_id), text, kb)
    else:
        await context.bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def send_draft_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, draft_id: str, header: str = "") -> None:
    sanitize_existing_draft_questions(draft_id)
    text, kb = _build_draft_detail_text_markup(user_id, draft_id, 0, header, context.bot_data.get('bot_username', ''))
    if hasattr(base, '_drop_home_panel_if_present'):
        await base._drop_home_panel_if_present(context, user_id)
    if hasattr(base, '_replace_single_panel_message'):
        await base._replace_single_panel_message(context, chat_id, ('ux-draft', user_id), text, kb)
    else:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


base.send_draft_card = send_draft_card


def _draft_question_rows_with_sections(draft_id: str) -> List[Dict[str, Any]]:
    sanitize_existing_draft_questions(draft_id)
    qrows = base.get_draft_questions(draft_id)
    sections = list_sections(draft_id)
    items: List[Dict[str, Any]] = []
    for row in qrows:
        qno = int(row['q_no'])
        section_title = 'General'
        for sec in sections:
            if int(sec['start_q']) <= qno <= int(sec['end_q']):
                section_title = base.normalize_visual_text(sec['title'] or 'General') or 'General'
                break
        items.append({
            'q_no': qno,
            'question': str(row['question']),
            'options': base.jload(row['options'], []) or [],
            'correct_option': int(row['correct_option']),
            'explanation': str(row['explanation'] or ''),
            'section': section_title,
        })
    return items


def render_scroll_exam_html(draft: Any, owner_id: int) -> str:
    theme = _current_creator_theme(owner_id)
    questions = _draft_question_rows_with_sections(str(draft['id']))
    if not questions:
        raise ValueError('Draft has no valid questions.')
    sections = sorted({q['section'] for q in questions})
    data = []
    for q in questions:
        data.append({
            'q_no': q['q_no'],
            'question': q['question'],
            'options': q['options'],
            'correct': q['correct_option'],
            'explanation': q['explanation'],
            'section': q['section'],
        })
    js_data = json.dumps(data, ensure_ascii=False)
    js_sections = json.dumps(sections, ensure_ascii=False)
    title = base.html_escape(base.normalize_visual_text(draft['title']))
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>window.MathJax={{tex:{{inlineMath:[['\\(','\\)'],['$','$']]}},svg:{{fontCache:'global'}}}};</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
<style>
:root{{--bg:{theme['bg']};--card:{theme['table']};--text:{theme['text']};--muted:{theme['muted']};--accent:{theme['accent']};--sub:{theme['subtext']};--outline:{theme['outline']};}}
*{{box-sizing:border-box}} body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Noto Sans,Arial;background:linear-gradient(135deg,var(--bg),#101827);color:var(--text)}}
.wrap{{max-width:1040px;margin:0 auto;padding:18px}} .card{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(16px);border-radius:20px;box-shadow:0 12px 40px rgba(0,0,0,.25)}}
.start{{padding:24px;display:grid;gap:14px}} .title{{font-size:28px;font-weight:900}} .muted{{color:var(--muted)}} .row{{display:flex;gap:12px;flex-wrap:wrap}} label.chip{{display:flex;align-items:center;gap:8px;padding:10px 14px;border-radius:12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10)}}
input[type=text]{{width:100%;padding:14px 16px;border-radius:14px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.15);color:var(--text)}} button{{cursor:pointer;border:0;border-radius:14px;padding:12px 16px;font-weight:800}} .primary{{background:var(--accent);color:#08111d}} .ghost{{background:rgba(255,255,255,.08);color:var(--text);border:1px solid rgba(255,255,255,.12)}}
.topbar{{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;gap:12px;align-items:center;padding:14px 18px;margin-bottom:14px}} .topbar.card{{border-radius:16px}} .qcard{{padding:18px;margin-bottom:14px;scroll-margin-top:90px}} .qidx{{font-weight:900;color:var(--accent)}} .qsec{{font-size:12px;color:var(--sub);padding:4px 10px;border:1px solid rgba(255,255,255,.10);border-radius:999px;display:inline-block;margin-left:10px}} .qtext{{font-size:18px;line-height:1.6;margin:12px 0 14px;white-space:pre-wrap}}
.opt{{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:14px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);margin:8px 0}} .opt.selected{{outline:2px solid var(--accent)}} .sticky-submit{{position:sticky;bottom:16px;display:flex;justify-content:flex-end;padding-top:12px}} .summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}} .stat{{padding:16px;border-radius:16px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08)}} .review{{display:grid;gap:12px}} .hidden{{display:none}}
</style></head><body>
<div class="wrap"><div id="startPage" class="card start"><div class="title">{title}</div><div class="muted">Questions: {len(questions)} • Time / question: {int(draft['question_time'])} sec • Negative: {draft['negative_mark']}</div><input id="studentName" type="text" placeholder="Enter your name"><div><div style="font-weight:800;margin-bottom:8px">Select sections</div><div id="sectionBox" class="row"></div></div><div class="row"><button id="startBtn" class="primary">Start HTML Exam</button><button id="themeBtn" class="ghost">Toggle Theme</button></div></div>
<div id="examPage" class="hidden"><div class="topbar card"><div><b>{title}</b><div class="muted" id="metaLine"></div></div><div><span class="muted">Remaining</span> <b id="timer">00:00</b></div></div><div id="questionsWrap"></div><div class="sticky-submit"><button id="submitBtn" class="primary">Submit Exam</button></div></div>
<div id="resultPage" class="hidden card start"><div class="title">Result</div><div id="resultSummary" class="summary"></div><div id="resultSections"></div><div id="reviewList" class="review"></div></div></div>
<script>
const QUESTIONS = {js_data}; const SECTIONS = {js_sections};
let selectedSections = new Set(SECTIONS); let active = []; let answers = {{}}; let themeDark = true; let totalSec = 0; let leftSec = 0; let timer = null;
const startPage = document.getElementById('startPage'); const examPage = document.getElementById('examPage'); const resultPage = document.getElementById('resultPage');
const sectionBox = document.getElementById('sectionBox'); const qWrap = document.getElementById('questionsWrap');
function renderSections(){{ sectionBox.innerHTML=''; SECTIONS.forEach(s=>{{ const l=document.createElement('label'); l.className='chip'; l.innerHTML=`<input type="checkbox" checked data-sec="${{s}}"> <span>${{s}}</span>`; l.querySelector('input').onchange=(e)=>{{ if(e.target.checked) selectedSections.add(s); else selectedSections.delete(s); }}; sectionBox.appendChild(l); }}); }}
function switchTheme(){{ themeDark=!themeDark; document.body.style.filter = themeDark ? 'none' : 'invert(1) hue-rotate(180deg)'; }}
document.getElementById('themeBtn').onclick=switchTheme;
function fmt(sec){{ sec=Math.max(0,Math.floor(sec)); const m=Math.floor(sec/60); const s=sec%60; return `${{String(m).padStart(2,'0')}}:${{String(s).padStart(2,'0')}}`; }}
function buildExam(){{ active = QUESTIONS.filter(q=>selectedSections.has(q.section)); if(!active.length) active = [...QUESTIONS]; qWrap.innerHTML=''; answers={{}}; totalSec = active.length * {int(draft['question_time'])}; leftSec = totalSec; document.getElementById('metaLine').textContent=`${{active.length}} questions • sections: ${{[...selectedSections].join(', ')||'All'}}`; active.forEach((q, idx)=>{{ const card=document.createElement('div'); card.className='card qcard'; card.id=`q${{idx}}`; const opts = q.options.map((opt,i)=>`<label class="opt" data-idx="${{idx}}" data-opt="${{i}}"><input type="radio" name="q${{idx}}"> <div><div><b>${{String.fromCharCode(65+i)}}.</b> ${{opt}}</div></div></label>`).join(''); card.innerHTML=`<div><span class="qidx">[${{idx+1}}/${{active.length}}]</span><span class="qsec">${{q.section}}</span></div><div class="qtext">${{q.question}}</div><div>${{opts}}</div>`; qWrap.appendChild(card); }}); qWrap.querySelectorAll('.opt').forEach(el=>{{ el.onclick=()=>{{ const idx=Number(el.dataset.idx), opt=Number(el.dataset.opt); answers[idx]=opt; document.querySelectorAll(`.opt[data-idx="${{idx}}"]`).forEach(x=>x.classList.remove('selected')); el.classList.add('selected'); const next=document.getElementById(`q${{idx+1}}`); if(next) next.scrollIntoView({{behavior:'smooth',block:'start'}}); }}; }}); if(window.MathJax) setTimeout(()=>window.MathJax.typesetPromise&&window.MathJax.typesetPromise(), 50); }}
function startTimer(){{ clearInterval(timer); document.getElementById('timer').textContent=fmt(leftSec); timer=setInterval(()=>{{ leftSec--; document.getElementById('timer').textContent=fmt(leftSec); if(leftSec<=0){{ clearInterval(timer); finishExam(); }} }},1000); }}
function finishExam(){{ clearInterval(timer); let c=0,w=0,s=0; const neg={float(draft['negative_mark'])}; active.forEach((q,idx)=>{{ if(!(idx in answers)) s++; else if(Number(answers[idx])===Number(q.correct)) c++; else w++; }}); const score = Math.round(((c*1)-(w*neg))*100)/100; startPage.classList.add('hidden'); examPage.classList.add('hidden'); resultPage.classList.remove('hidden'); const summary=[['Score',score],['Correct',c],['Wrong',w],['Skipped',s],['Accuracy',((c/Math.max(1,c+w))*100).toFixed(2)+'%'],['Time Used', Math.round((totalSec-leftSec)/60)+' min']]; document.getElementById('resultSummary').innerHTML = summary.map(x=>`<div class="stat"><div class="muted">${{x[0]}}</div><div style="font-size:26px;font-weight:900">${{x[1]}}</div></div>`).join(''); const secMap={{}}; active.forEach((q,idx)=>{{ secMap[q.section]=secMap[q.section]||{{total:0,c:0,w:0,s:0}}; secMap[q.section].total++; if(!(idx in answers)) secMap[q.section].s++; else if(Number(answers[idx])===Number(q.correct)) secMap[q.section].c++; else secMap[q.section].w++; }}); document.getElementById('resultSections').innerHTML = '<h3>Section Analysis</h3>'+Object.entries(secMap).map(([k,v])=>`<div class="stat" style="margin-top:10px"><b>${{k}}</b><div class="muted">Correct: ${{v.c}} • Wrong: ${{v.w}} • Skipped: ${{v.s}}</div></div>`).join(''); document.getElementById('reviewList').innerHTML = '<h3>Review</h3>'+active.map((q,idx)=>{{ const ans = answers[idx]; const chosen = ans===undefined ? 'Skipped' : q.options[ans]; return `<div class="stat"><div><b>Q${{idx+1}}</b> • ${{q.section}}</div><div style="margin:8px 0;white-space:pre-wrap">${{q.question}}</div><div>Your answer: <b>${{chosen}}</b></div><div>Correct answer: <b>${{q.options[q.correct]}}</b></div>${{q.explanation ? `<div class="muted" style="margin-top:8px">${{q.explanation}}</div>`:''}}</div>`; }}).join(''); if(window.MathJax) setTimeout(()=>window.MathJax.typesetPromise&&window.MathJax.typesetPromise(), 50); }}
document.getElementById('startBtn').onclick=()=>{{ buildExam(); startPage.classList.add('hidden'); examPage.classList.remove('hidden'); resultPage.classList.add('hidden'); startTimer(); window.scrollTo({{top:0,behavior:'smooth'}}); }};
document.getElementById('submitBtn').onclick=finishExam; renderSections();
</script></body></html>'''


def render_user_result_html(session: Any, participant_row: Any, rank_item: Dict[str, Any], ranking: List[Dict[str, Any]], review_items: List[Dict[str, Any]], section_items: List[Dict[str, Any]]) -> str:
    theme = _current_creator_theme(int(session['created_by']))
    name = base.html_escape(base.normalize_visual_text(participant_row['display_name'] or 'Student'))
    total_users = max(1, len(ranking))
    total_questions = int(session['total_questions'])
    correct = int(rank_item['correct'])
    wrong = int(rank_item['wrong'])
    skipped = int(rank_item['skipped'])
    score = base.html_escape(str(rank_item['score']))
    attempted = max(1, correct + wrong)
    accuracy = (correct / attempted) * 100.0
    percentage = (correct / max(1, total_questions)) * 100.0
    percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
    top_rows = []
    for item in ranking[:15]:
        display = base.html_escape(item['name'] + (f" {item['sub_name']}" if item.get('sub_name') else ''))
        active = ' style="background:rgba(255,255,255,.08);"' if int(item['user_id']) == int(participant_row['user_id']) else ''
        top_rows.append(f"<tr{active}><td>{item['rank']}</td><td>{display}</td><td>{item['correct']}</td><td>{item['wrong']}</td><td>{item['skipped']}</td><td>{base.html_escape(str(item['score']))}</td></tr>")
    review_html = []
    for item in review_items:
        status = item['status']
        border = {'correct':'#22c55e','wrong':'#ef4444','skipped':'#f59e0b'}.get(status,'#64748b')
        review_extra = ""
        if item['explanation']:
            review_extra = (
                "<div class='muted' style='margin-top:6px'>"
                + base.html_escape(item['explanation'])
                + "</div>"
            )
        review_html.append(
            f"<div class='review' style='border-left:4px solid {border}'><div class='muted'>Q{item['q_no']} • {base.html_escape(item['section'])} • {base.html_escape(status.title())}</div><div class='q'>{base.html_escape(item['question'])}</div><div>Your answer: <b>{base.html_escape(item['chosen'])}</b></div><div>Correct answer: <b>{base.html_escape(item['correct'])}</b></div>{review_extra}</div>"
        )
    section_html = ''.join(
        f"<div class='metric'><div class='muted'>{base.html_escape(item['title'])}</div><div class='value'>{item['correct']}/{item['total']}</div><div class='muted'>Wrong {item['wrong']} • Skipped {item['skipped']}</div></div>" for item in section_items
    )
    title = base.html_escape(base.normalize_visual_text(session['title']))
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Result</title>
<style>:root{{--bg:{theme['bg']};--text:{theme['text']};--muted:{theme['muted']};--card:{theme['table']};--accent:{theme['accent']};--sub:{theme['subtext']};}}*{{box-sizing:border-box}}body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:linear-gradient(135deg,var(--bg),#101827);color:var(--text)}}.wrap{{max-width:1100px;margin:0 auto;padding:22px}}.card{{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.10);backdrop-filter:blur(16px);border-radius:22px;box-shadow:0 12px 40px rgba(0,0,0,.25);padding:20px}}.title{{font-size:30px;font-weight:900;margin-bottom:6px}}.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-top:18px}}.metric{{padding:16px;border-radius:18px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08)}}.value{{font-size:30px;font-weight:900;margin-top:6px}}table{{width:100%;border-collapse:separate;border-spacing:0 10px;margin-top:12px}}th,td{{padding:12px 14px;text-align:left}}thead th{{color:var(--sub);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}tbody tr{{background:rgba(255,255,255,.04)}}tbody td:first-child{{border-top-left-radius:14px;border-bottom-left-radius:14px}}tbody td:last-child{{border-top-right-radius:14px;border-bottom-right-radius:14px}}.two{{display:grid;grid-template-columns:1.1fr .9fr;gap:16px;margin-top:18px}}@media(max-width:900px){{.two{{grid-template-columns:1fr}}}}.review{{padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.04);margin-bottom:12px}}.q{{font-size:16px;line-height:1.55;margin:8px 0 10px;white-space:pre-wrap}}</style></head><body><div class="wrap"><div class="card"><div class="title">{title}</div><div class="muted">Professional result report for {name}</div><div class="grid"><div class="metric"><div class="muted">Rank</div><div class="value">#{rank_item['rank']}/{total_users}</div></div><div class="metric"><div class="muted">Score</div><div class="value">{score}</div></div><div class="metric"><div class="muted">Accuracy</div><div class="value">{accuracy:.2f}%</div></div><div class="metric"><div class="muted">Percentage</div><div class="value">{percentage:.2f}%</div></div><div class="metric"><div class="muted">Percentile</div><div class="value">{percentile:.2f}</div></div><div class="metric"><div class="muted">Negative / wrong</div><div class="value">{session['negative_mark']}</div></div></div></div><div class="two"><div class="card"><div style="font-size:20px;font-weight:900">Ranking board</div><table><thead><tr><th>#</th><th>Name</th><th>Correct</th><th>Wrong</th><th>Skipped</th><th>Score</th></tr></thead><tbody>{''.join(top_rows)}</tbody></table></div><div class="card"><div style="font-size:20px;font-weight:900">Section analysis</div><div class="grid" style="margin-top:12px">{section_html}</div></div></div><div class="card" style="margin-top:18px"><div style="font-size:20px;font-weight:900;margin-bottom:12px">Detailed review</div>{''.join(review_html)}</div></div></body></html>'''


def _section_breakdown_for_user(session_id: str, user_id: int) -> List[Dict[str, Any]]:
    qrows = base.DBH.fetchall('SELECT q_no, section_title FROM session_questions WHERE session_id=? ORDER BY q_no', (session_id,))
    answers = {int(r['q_no']): r for r in base.DBH.fetchall('SELECT * FROM answers WHERE session_id=? AND user_id=?', (session_id, user_id))}
    grouped: Dict[str, Dict[str, Any]] = {}
    for q in qrows:
        sec = base.normalize_visual_text(q['section_title'] or '') or 'General'
        bucket = grouped.setdefault(sec, {'title': sec, 'total': 0, 'correct': 0, 'wrong': 0, 'skipped': 0})
        bucket['total'] += 1
        ans = answers.get(int(q['q_no']))
        if not ans:
            bucket['skipped'] += 1
        elif int(ans['is_correct']) == 1:
            bucket['correct'] += 1
        else:
            bucket['wrong'] += 1
    return list(grouped.values())


def _user_review_items(session_id: str, user_id: int) -> List[Dict[str, Any]]:
    qrows = base.DBH.fetchall('SELECT q_no, question, explanation, options, correct_option, section_title FROM session_questions WHERE session_id=? ORDER BY q_no', (session_id,))
    answers = {int(r['q_no']): r for r in base.DBH.fetchall('SELECT * FROM answers WHERE session_id=? AND user_id=?', (session_id, user_id))}
    items: List[Dict[str, Any]] = []
    for q in qrows:
        opts = base.jload(q['options'], []) or []
        ans = answers.get(int(q['q_no']))
        if not ans:
            status = 'skipped'
            chosen = 'Skipped'
        elif int(ans['is_correct']) == 1:
            status = 'correct'
            chosen = str(opts[int(ans['selected_option'])]) if 0 <= int(ans['selected_option']) < len(opts) else '—'
        else:
            status = 'wrong'
            chosen = str(opts[int(ans['selected_option'])]) if 0 <= int(ans['selected_option']) < len(opts) else '—'
        correct_text = str(opts[int(q['correct_option'])]) if 0 <= int(q['correct_option']) < len(opts) else '—'
        items.append({
            'q_no': int(q['q_no']),
            'question': str(q['question']),
            'chosen': chosen,
            'correct': correct_text,
            'explanation': str(q['explanation'] or ''),
            'section': base.normalize_visual_text(q['section_title'] or '') or 'General',
            'status': status,
        })
    return items


_prev_send_private_results_v4 = base.send_private_results

async def send_private_results(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session:
        return
    chat_row = base.DBH.fetchone("SELECT username FROM known_chats WHERE chat_id=?", (session['chat_id'],))
    username = chat_row['username'] if chat_row else None
    ranking = base.get_session_ranking(session_id)
    rank_map = {int(r['user_id']): r for r in ranking}
    participants = base.DBH.fetchall('SELECT * FROM participants WHERE session_id=? AND eligible=1', (session_id,))
    total_users = max(1, len(ranking))
    for p in participants:
        user_id = int(p['user_id'])
        row = base.DBH.fetchone('SELECT started FROM known_users WHERE user_id=?', (user_id,))
        if not row or int(row['started'] or 0) != 1:
            continue
        if not await base.is_required_channel_member(context, user_id):
            continue
        rank_item = rank_map.get(user_id)
        if not rank_item:
            continue
        section_data = _section_breakdown_for_user(session_id, user_id)
        review_items = _user_review_items(session_id, user_id)
        correct_links, wrong_links, skipped_links = [], [], []
        for item in review_items:
            q_no = item['q_no']
            qrow = base.DBH.fetchone('SELECT message_id FROM session_questions WHERE session_id=? AND q_no=?', (session_id, q_no))
            link = base.get_message_link(int(session['chat_id']), int(qrow['message_id'] or 0), username) if qrow else None
            label = f"<a href=\"{link}\">Q{q_no}</a>" if link else f"Q{q_no}"
            if item['status'] == 'correct':
                correct_links.append(label)
            elif item['status'] == 'wrong':
                wrong_links.append(label)
            else:
                skipped_links.append(label)
        correct = int(rank_item['correct'])
        wrong = int(rank_item['wrong'])
        skipped = int(rank_item['skipped'])
        attempted = max(1, correct + wrong)
        accuracy = (correct / attempted) * 100.0
        percentage = (correct / max(1, int(session['total_questions']))) * 100.0
        percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
        section_lines: List[str] = []
        if section_data and (len(section_data) > 1 or section_data[0]['title'] != 'General'):
            section_lines.append('<b>Section Analysis</b>')
            for item in section_data:
                section_lines.append(f"• {base.html_escape(item['title'])}: ✅ {item['correct']}  ❌ {item['wrong']}  ➖ {item['skipped']}")
            section_lines.append('')
        message_text = (
            f"<b>{base.html_escape(session['title'])}</b>\n"
            f"Rank: <b>#{rank_item['rank']}</b> / {total_users}\n"
            f"Score: <b>{rank_item['score']}</b>    Negative: <b>{session['negative_mark']}</b>\n"
            f"✅ Correct: <b>{correct}</b>    ❌ Wrong: <b>{wrong}</b>    ➖ Skipped: <b>{skipped}</b>\n"
            f"Accuracy: <b>{accuracy:.2f}%</b>    Percentage: <b>{percentage:.2f}%</b>    Percentile: <b>{percentile:.2f}</b>\n\n"
            + ("\n".join(section_lines) if section_lines else "") +
            f"<b>Correct</b>\n{', '.join(correct_links) or '—'}\n\n"
            f"<b>Wrong</b>\n{', '.join(wrong_links) or '—'}\n\n"
            f"<b>Skipped</b>\n{', '.join(skipped_links) or '—'}"
        )
        buttons: List[List[InlineKeyboardButton]] = []
        practice_url = _build_practice_url_v4(context.bot_data.get('bot_username', ''), str(session['draft_id']), int(session['created_by']))
        if practice_url:
            buttons.append([InlineKeyboardButton('🔁 Try Again', url=practice_url)])
        with suppress(TelegramError):
            await context.bot.send_message(user_id, message_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, disable_web_page_preview=True)
        with suppress(Exception):
            html_doc = render_user_result_html(session, p, rank_item, ranking, review_items, section_data)
            await context.bot.send_document(
                user_id,
                document=InputFile(base.io.BytesIO(html_doc.encode('utf-8')), filename=f"{base.pdf_safe_filename(session['title'])}_result.html"),
                caption='Website-style result report.',
            )


base.send_private_results = send_private_results


_prev_create_session_v4 = base.create_session_from_draft

def create_session_from_draft(draft_id: str, chat_id: int, actor_id: int) -> Optional[str]:
    sanitize_existing_draft_questions(draft_id)
    session_id = _prev_create_session_v4(draft_id, chat_id, actor_id)
    if session_id:
        apply_sections_to_session(session_id, draft_id)
        base.DBH.execute("UPDATE sessions SET speed_factor=COALESCE(speed_factor,1.0), speed_mode=COALESCE(speed_mode,'normal'), paused_at=NULL WHERE id=?", (session_id,))
    return session_id


base.create_session_from_draft = create_session_from_draft


async def begin_or_advance_exam(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session or session['status'] != 'running':
        return
    total = int(session['total_questions'] or 0)
    next_index = int(session['current_index'] or 0) + 1
    while next_index <= total:
        q = base.get_session_question(session_id, next_index)
        if not q:
            next_index += 1
            continue
        raw_opts = base.jload(q['options'], []) or []
        clean_opts, new_correct = _clean_and_map_options(raw_opts, int(q['correct_option']))
        q_text = _smart_clean_question_text(str(q['question'] or ''))
        if not q_text or len(clean_opts) < 2 or new_correct is None:
            base.DBH.execute("UPDATE sessions SET current_index=? WHERE id=?", (next_index, session_id))
            next_index += 1
            continue
        section_title = base.normalize_visual_text(q['section_title'] or '')
        draft_row = base.get_draft(str(session['draft_id']))
        show_title = True if not draft_row else _draft_prefix_state(draft_row)
        base_seconds = int(q['question_time_override'] or session['question_time'] or 30)
        speed_factor = float(session['speed_factor'] or 1.0)
        effective_seconds = max(5, int(round(base_seconds * speed_factor)))
        prefix_parts = [f"[{next_index}/{total}]"]
        if section_title:
            prefix_parts.append(f"[{section_title}]")
        if show_title:
            prefix_parts.append(f"[{base.normalize_visual_text(session['title'])}]")
        question_prefix = (' '.join(prefix_parts) + '\n') if prefix_parts else ''
        poll_question = (question_prefix + q_text).strip() or q_text or f"Question {next_index}"
        if len(poll_question) > 300:
            allowed_q = max(10, 300 - len(question_prefix))
            poll_question = question_prefix + q_text[: allowed_q - 1].rstrip() + '…'
        explanation_text = _smart_clean_explanation_text(q['explanation'] or f"Question {next_index} of {total}")
        if len(explanation_text) > 200:
            explanation_text = explanation_text[:199] + '…'
        try:
            msg = await context.bot.send_poll(
                chat_id=session['chat_id'],
                question=poll_question,
                options=clean_opts,
                type=Poll.QUIZ,
                is_anonymous=False,
                allows_multiple_answers=False,
                correct_option_id=int(new_correct),
                explanation=explanation_text or f"Question {next_index} of {total}",
                open_period=effective_seconds,
            )
        except TelegramError as exc:
            base.logger.exception('Failed to send poll: %s', exc)
            next_index += 1
            if next_index > total:
                await base.finish_exam(context, session_id, reason='completed')
            else:
                context.job_queue.run_once(base.begin_or_advance_exam_job, when=0.4, data={'session_id': session_id}, name=f'advance:{session_id}:recover')
            return
        with closing(base.DBH.connect()) as conn:
            conn.execute(
                'UPDATE session_questions SET question=?, options=?, correct_option=?, poll_id=?, message_id=?, open_ts=?, close_ts=? WHERE session_id=? AND q_no=?',
                (q_text, base.jdump(clean_opts), int(new_correct), msg.poll.id, msg.message_id, base.now_ts(), base.now_ts() + effective_seconds, session_id, next_index),
            )
            conn.execute('UPDATE sessions SET current_index=?, active_poll_id=?, active_poll_message_id=? WHERE id=?', (next_index, msg.poll.id, msg.message_id, session_id))
            conn.commit()
        context.job_queue.run_once(base.close_poll_job, when=max(1, effective_seconds), data={'session_id': session_id, 'q_no': next_index}, name=f'close:{session_id}:{next_index}')
        return
    await base.finish_exam(context, session_id, reason='completed')


base.begin_or_advance_exam = begin_or_advance_exam


_prev_handle_document_upload_v4 = base.handle_document_upload

async def handle_document_upload(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.document:
        return await _prev_handle_document_upload_v4(update, context)
    state, payload = base.get_user_state(user.id)
    lower_name = (message.document.file_name or '').lower()
    if chat.type == 'private' and base.user_has_staff_access(user.id):
        if state == 'adv2_add_questions' and lower_name.endswith(('.txt', '.md', '.json')):
            file = await message.document.get_file()
            data = bytes(await file.download_as_bytearray())
            clear_text = data.decode('utf-8-sig', errors='replace')
            draft_id = str(payload.get('draft_id') or '')
            base.clear_user_state(user.id)
            await import_text_into_draft(message, context, draft_id, clear_text, src=f"txt:{message.document.file_name or 'upload.txt'}")
            with suppress(Exception):
                await base.safe_delete_message(context.bot, chat.id, message.message_id)
            return
    result = await _prev_handle_document_upload_v4(update, context)
    if chat.type == 'private' and base.user_has_staff_access(user.id) and lower_name.endswith('.csv'):
        draft_id = base.get_active_draft_id(user.id)
        if draft_id:
            await send_draft_card(context, user.id, user.id, draft_id, header='✅ Draft updated from CSV import.')
            with suppress(Exception):
                await base.safe_delete_message(context.bot, chat.id, message.message_id)
    return result


base.handle_document_upload = handle_document_upload


_prev_handle_poll_import_v4 = base.handle_poll_import

async def handle_poll_import(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.poll:
        return await _prev_handle_poll_import_v4(update, context)
    if chat.type == 'private' and base.user_has_staff_access(user.id):
        clone = get_clone_session(user.id)
        draft_id = str(clone['draft_id']) if clone else (base.get_active_draft_id(user.id) or '')
        if draft_id and message.poll.type == Poll.QUIZ and message.poll.correct_option_id is not None:
            ok, q_no = dedup_add_question_to_draft(
                draft_id,
                message.poll.question,
                [opt.text for opt in message.poll.options],
                int(message.poll.correct_option_id),
                message.poll.explanation or '',
                'quizbot_clone' if clone else 'forwarded_quiz',
            )
            sanitize_existing_draft_questions(draft_id)
            header = f"✅ {'Clone' if clone else 'Draft'} updated. Added question Q{q_no}." if ok and q_no else 'ℹ️ Duplicate or invalid question skipped.'
            await send_draft_card(context, user.id, user.id, draft_id, header=header)
            with suppress(Exception):
                await base.safe_delete_message(context.bot, chat.id, message.message_id)
            return
    return await _prev_handle_poll_import_v4(update, context)


base.handle_poll_import = handle_poll_import


async def _show_prompt(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if hasattr(base, '_replace_single_panel_message'):
        await base._replace_single_panel_message(context, user_id, ('ux-prompt', user_id), text, reply_markup)
    else:
        await context.bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True)


_prev_callback_router_v4 = base.callback_router

async def callback_router(update: Update, context) -> None:
    query = update.callback_query
    if not query or not query.data:
        return await _prev_callback_router_v4(update, context)
    data = query.data
    user = query.from_user
    if user:
        base.record_user(user)
    if data == 'panel:drafts' or data.startswith('ux:'):
        await query.answer()
        if not user or not base.user_has_staff_access(user.id):
            warn_kb = InlineKeyboardMarkup([[InlineKeyboardButton('📘 Commands', callback_data='panel:commands')]])
            await base.panel_show_message(query.message, user.id if user else 0, base.warning_text(), reply_markup=warn_kb)
            return
        parts = data.split(':')
        action = parts[1] if len(parts) > 1 else ''
        if data == 'panel:drafts' or action == 'browse':
            try:
                page = int(parts[2]) if len(parts) > 2 else 0
            except Exception:
                page = 0
            text, kb = _build_draft_browser_list_text_markup(user.id, page)
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'open' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, bot_username=context.bot_data.get('bot_username', ''))
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'set' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = resolve_editable_draft(user.id, draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft not found or access denied.')
            else:
                base.set_active_draft(user.id, draft_id)
                text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, f'✅ Active draft set to <code>{draft_id}</code>.', context.bot_data.get('bot_username', ''))
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'del' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = base.get_draft(draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft already deleted.')
            elif int(draft['owner_id']) != user.id and not base.is_owner(user.id):
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Only the draft owner or bot owner can delete this draft.')
            else:
                base.delete_draft(draft_id, user.id)
                text, kb = _build_draft_browser_list_text_markup(user.id, page, f'🗑 Draft <code>{draft_id}</code> deleted.')
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'prefix' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = resolve_editable_draft(user.id, draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft not found or access denied.')
            else:
                new_val = 0 if _draft_prefix_state(draft) else 1
                base.DBH.execute('UPDATE drafts SET show_title_prefix=?, updated_at=? WHERE id=?', (new_val, base.now_ts(), draft_id))
                text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, f"✅ Title prefix turned <b>{'ON' if new_val else 'OFF'}</b>.", context.bot_data.get('bot_username', ''))
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'shuffle' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = resolve_editable_draft(user.id, draft_id)
            if draft:
                shuffle_draft_questions(draft_id)
                text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, '✅ Draft questions shuffled.', context.bot_data.get('bot_username', ''))
            else:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft not found or access denied.')
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action in {'ptitle','ptime','pneg','padd','pdelq','psection'} and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = resolve_editable_draft(user.id, draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft not found or access denied.')
                await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
                return
            prompt_map = {
                'ptitle': ('adv2_edit_title', 'Send the new title now.'),
                'ptime': ('adv2_edit_time', 'Send the time per question in seconds. Example: <code>30</code>'),
                'pneg': ('adv2_edit_neg', 'Send the negative mark now. Example: <code>0.25</code>'),
                'padd': ('adv2_add_questions', 'Send MCQ text now, or upload a TXT/MD/JSON file. The bot will add every valid question and keep the inbox clean.'),
                'pdelq': ('adv2_del_questions', 'Send question numbers to delete. Example: <code>3,5-7</code>'),
                'psection': ('adv2_add_section', 'Send the section range in this format:\n<code>1-10 | Biology | 30</code>'),
            }
            state_name, prompt = prompt_map[action]
            base.set_user_state(user.id, state_name, {'draft_id': draft_id, 'page': page})
            await _show_prompt(context, user.id, f"<b>{base.html_escape(draft['title'])}</b>\nCode: <code>{draft_id}</code>\n\n{prompt}")
            return
        if action == 'html' and len(parts) >= 4:
            draft_id, page = parts[2], int(parts[3])
            draft = resolve_editable_draft(user.id, draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, page, '⚠️ Draft not found or access denied.')
                await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
                return
            try:
                html_doc = render_scroll_exam_html(draft, int(draft['owner_id']))
                await context.bot.send_document(
                    user.id,
                    document=InputFile(base.io.BytesIO(html_doc.encode('utf-8')), filename=f"{base.pdf_safe_filename(draft['title'])}_practice.html"),
                    caption='Interactive HTML practice exam with section selection, auto-scroll navigation, and MathJax support.',
                )
                text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, '✅ HTML practice exam exported.', context.bot_data.get('bot_username', ''))
            except Exception as exc:
                text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, f'⚠️ HTML export failed: <code>{base.html_escape(str(exc))}</code>', context.bot_data.get('bot_username', ''))
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
    return await _prev_callback_router_v4(update, context)


base.callback_router = callback_router


_prev_handle_text_v5 = base.handle_text

async def handle_text(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not getattr(message, 'text', None):
        return await _prev_handle_text_v5(update, context)
    state, payload = base.get_user_state(user.id)
    cmd, args = base.extract_command(message.text, context.bot_data.get('bot_username', ''))
    cmd = (cmd or '').lower()

    if chat.type == 'private' and state in _ADV_EDIT_STATES and not cmd:
        draft_id = str(payload.get('draft_id') or '')
        page = int(payload.get('page') or 0)
        draft = resolve_editable_draft(user.id, draft_id)
        base.clear_user_state(user.id)
        if not draft:
            await _show_draft_browser(context, user.id, page=page, header='⚠️ Draft not found or access denied.')
            return
        txt = message.text.strip()
        header = ''
        if state == 'adv2_edit_title':
            if not txt:
                header = '⚠️ Title cannot be empty.'
            else:
                base.DBH.execute('UPDATE drafts SET title=?, updated_at=? WHERE id=?', (base.normalize_visual_text(txt), base.now_ts(), draft_id))
                header = '✅ Draft title updated.'
        elif state == 'adv2_edit_time':
            if not txt.isdigit():
                header = '⚠️ Send only a positive number.'
            else:
                secs = max(5, int(txt))
                base.DBH.execute('UPDATE drafts SET question_time=?, updated_at=? WHERE id=?', (secs, base.now_ts(), draft_id))
                header = f'✅ Default time updated to <b>{secs}</b> sec.'
        elif state == 'adv2_edit_neg':
            try:
                neg = float(txt)
                base.DBH.execute('UPDATE drafts SET negative_mark=?, updated_at=? WHERE id=?', (neg, base.now_ts(), draft_id))
                header = f'✅ Negative mark updated to <b>{neg}</b>.'
            except Exception:
                header = '⚠️ Send a valid decimal value. Example: <code>0.25</code>'
        elif state == 'adv2_add_questions':
            parsed = parse_marked_questions_from_text(txt)
            if not parsed:
                header = '⚠️ No valid questions were found in the text you sent.'
            else:
                added = 0
                for item in parsed:
                    ok, _q_no = dedup_add_question_to_draft(draft_id, item['question'], item['options'], int(item['correct_option']), item.get('explanation') or '', 'text_manual')
                    if ok:
                        added += 1
                sanitize_existing_draft_questions(draft_id)
                header = f'✅ Draft updated. Added <b>{added}</b> question(s).'
        elif state == 'adv2_del_questions':
            numbers = parse_q_number_list(txt)
            removed = delete_question_numbers(draft_id, numbers)
            sanitize_existing_draft_questions(draft_id)
            header = f'✅ Removed <b>{removed}</b> question(s).'
        elif state == 'adv2_add_section':
            parts = [x.strip() for x in txt.split('|')]
            if len(parts) < 2 or '-' not in parts[0]:
                header = '⚠️ Use: <code>1-10 | Biology | 30</code>'
            else:
                a, b = [x.strip() for x in parts[0].split('-', 1)]
                if not (a.isdigit() and b.isdigit()):
                    header = '⚠️ Use numeric ranges like <code>1-10</code>.'
                else:
                    title = parts[1] if len(parts) >= 2 else f'Section {a}-{b}'
                    q_time = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
                    set_section(draft_id, int(a), int(b), title, q_time)
                    header = f'✅ Section added: <b>{base.html_escape(title)}</b>.'
        with suppress(Exception):
            await base.safe_delete_message(context.bot, chat.id, message.message_id)
        text, kb = _build_draft_detail_text_markup(user.id, draft_id, page, header, context.bot_data.get('bot_username', ''))
        if hasattr(base, '_replace_single_panel_message'):
            await base._replace_single_panel_message(context, user.id, ('ux-draft', user.id), text, kb)
        else:
            await context.bot.send_message(user.id, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return

    if chat.type == 'private' and base.user_has_staff_access(user.id):
        if cmd in {'drafts', 'mydrafts'}:
            with suppress(Exception):
                await base.safe_delete_message(context.bot, chat.id, message.message_id)
            await _show_draft_browser(context, user.id)
            return
        if cmd in {'exporthtml', 'htmlexam'}:
            draft = resolve_editable_draft(user.id, args.strip())
            if not draft:
                await base.safe_reply(message, 'Select an active draft first, or pass the draft code: /exporthtml DRAFTCODE')
                return
            html_doc = render_scroll_exam_html(draft, int(draft['owner_id']))
            await context.bot.send_document(
                user.id,
                document=InputFile(base.io.BytesIO(html_doc.encode('utf-8')), filename=f"{base.pdf_safe_filename(draft['title'])}_practice.html"),
                caption='Interactive HTML practice exam exported.',
            )
            return
        if cmd == 'section' and not args.strip():
            draft_id = base.get_active_draft_id(user.id)
            if not draft_id:
                await base.safe_reply(message, 'Select an active draft first.')
                return
            base.set_user_state(user.id, 'adv2_add_section', {'draft_id': draft_id, 'page': 0})
            await _show_prompt(context, user.id, f"<b>Add Section</b>\nCode: <code>{draft_id}</code>\n\nSend: <code>1-10 | Biology | 30</code>")
            return
    return await _prev_handle_text_v5(update, context)


base.handle_text = handle_text


def admin_private_commands() -> List[BotCommand]:
    return everyone_private_commands() + [
        BotCommand('panel', 'Admin panel'),
        BotCommand('newexam', 'Create new exam draft'),
        BotCommand('drafts', 'Draft browser'),
        BotCommand('csvformat', 'CSV import format'),
        BotCommand('importtext', 'Import MCQs from text / TXT'),
        BotCommand('txtquiz', 'Alias of importtext'),
        BotCommand('clonequiz', 'Start QuizBot clone workflow'),
        BotCommand('cloneend', 'Finish clone workflow'),
        BotCommand('draftinfo', 'Show draft details'),
        BotCommand('settitle', 'Edit draft title'),
        BotCommand('settime', 'Edit time per question'),
        BotCommand('setneg', 'Edit negative marking'),
        BotCommand('shuffle', 'Shuffle draft questions'),
        BotCommand('delq', 'Delete question numbers'),
        BotCommand('section', 'Add section timing'),
        BotCommand('sections', 'List draft sections'),
        BotCommand('clearsections', 'Remove all sections'),
        BotCommand('exporthtml', 'Export HTML practice exam'),
        BotCommand('creator', 'Show draft creator info'),
        BotCommand('renamefile', 'Rename a file in bot inbox'),
        BotCommand('setthumb', 'Set preview thumbnail'),
        BotCommand('clearthumb', 'Clear thumbnail'),
        BotCommand('thumbstatus', 'Thumbnail status'),
        BotCommand('cancel', 'Cancel current input flow'),
    ]


def owner_private_commands() -> List[BotCommand]:
    return admin_private_commands() + [
        BotCommand('theme', 'Leaderboard theme settings'),
        BotCommand('addadmin', 'Add isolated admin'),
        BotCommand('addadminalp', 'Add all-access admin'),
        BotCommand('rmadmin', 'Remove admin'),
        BotCommand('admins', 'List admin roles'),
        BotCommand('audit', 'Recent admin actions'),
        BotCommand('logs', 'Bot logs summary'),
        BotCommand('broadcast', 'Broadcast to groups and users'),
        BotCommand('announce', 'Announce to one chat'),
        BotCommand('restart', 'Restart bot'),
    ]


base.admin_private_commands = admin_private_commands
base.owner_private_commands = owner_private_commands


# ============================================================
# Final UX patch v5 (stable)
# ============================================================
import io as _io
from textwrap import shorten as _shorten
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as _plt
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

_QUESTION_MANAGER_PAGE = 6


def _safe_int(value, default=0):
    try:
        return default if value is None else int(value)
    except Exception:
        return default


def _draft_prefix_state(draft: Any) -> bool:
    try:
        raw = draft['show_title_prefix']
    except Exception:
        raw = None
    if raw is None:
        return True
    try:
        return bool(int(raw))
    except Exception:
        return True


def _question_preview_line(row: Any) -> str:
    return _shorten(base.normalize_visual_text(str(row['question'] or '')).replace('\n', ' '), width=78, placeholder='...')


def _rebuild_questions_exact(draft_id: str, items: List[Dict[str, Any]]) -> None:
    with closing(base.DBH.connect()) as conn:
        conn.execute('DELETE FROM draft_questions WHERE draft_id=?', (draft_id,))
        for idx, item in enumerate(items, start=1):
            conn.execute(
                'INSERT INTO draft_questions(draft_id, q_no, question, options, correct_option, explanation, src) VALUES(?,?,?,?,?,?,?)',
                (draft_id, idx, item['question'], base.jdump(item['options']), int(item['correct_option']), item.get('explanation', ''), item.get('src', 'manual')),
            )
        conn.commit()
    base.refresh_draft_status(draft_id)


def insert_questions_into_draft(draft_id: str, items: List[Dict[str, Any]], insert_after: Optional[int] = None) -> int:
    current = []
    for row in base.get_draft_questions(draft_id):
        current.append({'question': str(row['question']), 'options': base.jload(row['options'], []) or [], 'correct_option': int(row['correct_option']), 'explanation': str(row['explanation'] or ''), 'src': str(row['src'] or 'manual')})
    pos = len(current) if insert_after is None else max(0, min(len(current), int(insert_after)))
    _rebuild_questions_exact(draft_id, current[:pos] + list(items) + current[pos:])
    sanitize_existing_draft_questions(draft_id)
    return len(items)


def _build_question_manager_text_markup(user_id: int, draft_id: str, page: int = 0, header: str = '') -> Tuple[str, InlineKeyboardMarkup]:
    draft = base.get_draft(draft_id)
    if not draft:
        return _build_draft_browser_list_text_markup(user_id, header='⚠️ Draft not found.')
    sanitize_existing_draft_questions(draft_id)
    rows = list(base.get_draft_questions(draft_id))
    total = len(rows)
    pages = max(1, (total + _QUESTION_MANAGER_PAGE - 1) // _QUESTION_MANAGER_PAGE) if total else 1
    page = max(0, min(int(page), pages - 1))
    start = page * _QUESTION_MANAGER_PAGE
    end = min(total, start + _QUESTION_MANAGER_PAGE)
    chunk = rows[start:end]
    lines = []
    if header:
        lines += [header, '']
    lines += ['<b>Question Manager</b>', f'Draft: <b>{base.html_escape(draft["title"])}</b>', f'Code: <code>{draft_id}</code>', f'Page <b>{page+1}/{pages}</b> • Questions <b>{start+1}-{end if total else 0}</b> of <b>{total}</b>', '']
    kb_rows = []
    for row in chunk:
        qno = int(row['q_no'])
        lines.append(f'<b>Q{qno}</b> — {base.html_escape(_question_preview_line(row))}')
        lines.append('')
        kb_rows.append([InlineKeyboardButton(f'➕ After Q{qno}', callback_data=f'uxq:add:{draft_id}:{qno}:{page}'), InlineKeyboardButton(f'🗑 Q{qno}', callback_data=f'uxq:del:{draft_id}:{qno}:{page}')])
    kb_rows.append([InlineKeyboardButton('➕ Add at Start', callback_data=f'uxq:add:{draft_id}:0:{page}'), InlineKeyboardButton('➕ Add at End', callback_data=f'uxq:add:{draft_id}:{total}:{page}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'uxq:browse:{draft_id}:{page-1}'))
    if page < pages - 1:
        nav.append(InlineKeyboardButton('➡️ Next', callback_data=f'uxq:browse:{draft_id}:{page+1}'))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton('📂 Draft Detail', callback_data=f'ux:open:{draft_id}:0'), InlineKeyboardButton('📚 Draft Browser', callback_data='ux:browse:0')])
    return '\n'.join(lines).strip(), InlineKeyboardMarkup(kb_rows)


def _build_draft_browser_list_text_markup(user_id: int, page: int = 0, header: str = '') -> Tuple[str, InlineKeyboardMarkup]:
    drafts = list(base.list_user_drafts(user_id))
    drafts = sorted(drafts, key=lambda r: int(r['updated_at']), reverse=True)
    if not drafts:
        text = ((header + '\n\n') if header else '') + '<b>Your Draft Browser</b>\n\nNo drafts yet.'
        return text, InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ Back', callback_data='panel:home')]])
    page = _clamp_draft_page_v4(page, len(drafts))
    start = page * DRAFTS_PAGE_SIZE_V4
    end = min(start + DRAFTS_PAGE_SIZE_V4, len(drafts))
    page_rows = drafts[start:end]
    total_pages = max(1, (len(drafts) + DRAFTS_PAGE_SIZE_V4 - 1) // DRAFTS_PAGE_SIZE_V4)
    active_id = base.get_active_draft_id(user_id)
    lines = []
    if header:
        lines += [header, '']
    lines += ['<b>Your Draft Browser</b>', f'Page <b>{page+1}/{total_pages}</b> • Total <b>{len(drafts)}</b>', '']
    kb_rows = []
    for idx, row in enumerate(page_rows, start=start + 1):
        prefix = 'ON' if _draft_prefix_state(row) else 'OFF'
        status = 'ACTIVE' if active_id == row['id'] else ('Ready' if _safe_int(row['q_count']) > 0 else 'Draft')
        lines.append(f'<b>{idx}. {base.html_escape(row["title"])}</b>')
        lines.append(f'Code: <code>{row["id"]}</code> • Q: <b>{row["q_count"]}</b> • {row["question_time"]} sec • -{row["negative_mark"]} • Prefix {prefix} • {status}')
        lines.append('')
        kb_rows.append([InlineKeyboardButton(f'📂 {row["id"]}', callback_data=f'ux:open:{row["id"]}:{page}'), InlineKeyboardButton('✅ Active' if active_id == row['id'] else '🔄 Active', callback_data=f'ux:set:{row["id"]}:{page}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Previous', callback_data=f'ux:browse:{page-1}'))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton('➡️ Next', callback_data=f'ux:browse:{page+1}'))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton('⬅️ Back', callback_data='panel:home')])
    return '\n'.join(lines).strip(), InlineKeyboardMarkup(kb_rows)


def _build_draft_detail_text_markup(user_id: int, draft_id: str, page: int = 0, header: str = '', bot_username: str = '') -> Tuple[str, InlineKeyboardMarkup]:
    draft = base.get_draft(draft_id)
    if not draft:
        return _build_draft_browser_list_text_markup(user_id, page=page, header='⚠️ Draft not found.')
    sanitize_existing_draft_questions(draft_id)
    draft = base.get_draft(draft_id)
    q_count = _calc_total_draft_questions(draft_id)
    lines = []
    if header:
        lines += [header, '']
    lines += ['<b>Draft Details</b>', f'Title: <b>{base.html_escape(draft["title"])}</b>', f'Code: <code>{draft["id"]}</code>', f'Questions: <b>{q_count}</b>', f'Time / question: <b>{draft["question_time"]} sec</b>', f'Negative / wrong: <b>{draft["negative_mark"]}</b>', f'Title prefix in poll: <b>{"ON" if _draft_prefix_state(draft) else "OFF"}</b>', f'Created: <b>{base.fmt_dt(draft["created_at"])}</b>', f'Updated: <b>{base.fmt_dt(draft["updated_at"])}</b>', '', '<b>Sections</b>', *_section_summary_for_draft(draft_id)]
    practice_url = _build_practice_url_v4(bot_username, draft_id, int(draft['owner_id'])) if q_count > 0 else None
    kb_rows = [
        [InlineKeyboardButton('📂 Open Draft', callback_data=f'ux:open:{draft_id}:{page}'), InlineKeyboardButton('🔄 Set Active', callback_data=f'ux:set:{draft_id}:{page}')],
        [InlineKeyboardButton(f'⚙️ Prefix {"OFF" if _draft_prefix_state(draft) else "ON"}', callback_data=f'ux:prefix:{draft_id}:{page}'), InlineKeyboardButton('🎲 Shuffle', callback_data=f'ux:shuffle:{draft_id}:{page}')],
        [InlineKeyboardButton('✏️ Edit Title', callback_data=f'ux:ptitle:{draft_id}:{page}'), InlineKeyboardButton('⏱ Edit Time', callback_data=f'ux:ptime:{draft_id}:{page}')],
        [InlineKeyboardButton('➖ Edit Negative', callback_data=f'ux:pneg:{draft_id}:{page}'), InlineKeyboardButton('🧩 Manage Questions', callback_data=f'uxq:browse:{draft_id}:0')],
        [InlineKeyboardButton('📚 Sections', callback_data=f'ux:psection:{draft_id}:{page}'), InlineKeyboardButton('🌐 HTML Export', callback_data=f'ux:html:{draft_id}:{page}')],
    ]
    if practice_url:
        kb_rows.append([InlineKeyboardButton('🧪 Practice Link', url=practice_url)])
    kb_rows.append([InlineKeyboardButton('🗑 Delete Draft', callback_data=f'ux:del:{draft_id}:{page}'), InlineKeyboardButton('📚 Draft Browser', callback_data=f'ux:browse:{page}')])
    return '\n'.join(lines).strip(), InlineKeyboardMarkup(kb_rows)


def _extract_insert_position_from_text(text: str) -> Tuple[Optional[int], str]:
    lines = (text or '').splitlines()
    if not lines:
        return None, text or ''
    m = re.match(r'^(?:@|after\s*q?|pos(?:ition)?\s*:?)\s*(\d+)$', lines[0].strip(), flags=re.I)
    if m:
        return int(m.group(1)), '\n'.join(lines[1:]).strip()
    return None, text or ''


def _contains_math_markup(text: str) -> bool:
    return bool(text and re.search(r'(\\frac|\\sqrt|\\left|\\right|\\pi|\\theta|\\alpha|\\beta|\\gamma|\\lim|\\int|\\sum|\$|\\\(|\\\)|\^|_)', text))


def _normalize_math_text(text: str) -> str:
    return (text or '').replace('\\(', '$').replace('\\)', '$').replace('\\[', '$$').replace('\\]', '$$')


def _render_math_question_image(question: str, options: List[str]) -> Optional[bytes]:
    if not _HAS_MPL:
        return None
    q = _normalize_math_text(question)
    opts = [_normalize_math_text(x) for x in options]
    if not (_contains_math_markup(q) or any(_contains_math_markup(x) for x in opts)):
        return None
    try:
        fig = _plt.figure(figsize=(10, max(4.0, min(12.0, 2.8 + 0.5 * len(opts)))), dpi=180)
        fig.patch.set_facecolor('white')
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis('off')
        ax.text(0.04, 0.94, q, fontsize=17, va='top', ha='left', wrap=True)
        y = 0.72
        for idx, opt in enumerate(opts):
            ax.text(0.06, y, f'{chr(65+idx)}. {opt}', fontsize=15, va='top', ha='left', wrap=True)
            y -= 0.12
        buf = _io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.25)
        _plt.close(fig)
        return buf.getvalue()
    except Exception:
        try:
            _plt.close('all')
        except Exception:
            pass
        return None


_old_render_scroll_exam_html = render_scroll_exam_html

def render_scroll_exam_html(draft: Any, owner_id: int) -> str:
    html = _old_render_scroll_exam_html(draft, owner_id)
    theme_mode = 'dark' if str(getattr(draft, 'html_export_theme', None) or draft['html_export_theme'] or 'auto').lower() == 'dark' else 'light'
    inject_css = "body[data-theme='light']{filter:none!important;}body[data-theme='dark']{filter:none!important;background:#0f172a!important;color:#f8fafc!important}.opt.locked{cursor:not-allowed!important;opacity:.92}"
    html = html.replace('</style></head>', inject_css + '</style></head>', 1)
    html = html.replace("let selectedSections = new Set(SECTIONS); let active = []; let answers = {}; let themeDark = true; let totalSec = 0; let leftSec = 0; let timer = null;", f"let selectedSections = new Set(SECTIONS); let active = []; let answers = {{}}; let themeDark = {'true' if theme_mode=='dark' else 'false'}; let totalSec = 0; let leftSec = 0; let timer = null; document.body.setAttribute('data-theme', themeDark ? 'dark' : 'light');")
    html = html.replace("function switchTheme(){ themeDark=!themeDark; document.body.style.filter = themeDark ? 'none' : 'invert(1) hue-rotate(180deg)'; }", "function switchTheme(){ themeDark=!themeDark; document.body.setAttribute('data-theme', themeDark ? 'dark' : 'light'); }")
    html = html.replace("document.querySelectorAll(`.opt[data-idx=\"${idx}\"]`).forEach(x=>x.classList.remove('selected')); el.classList.add('selected');", "if(answers[idx]!==undefined) return; answers[idx]=opt; document.querySelectorAll(`.opt[data-idx=\"${idx}\"]`).forEach(x=>x.classList.add('locked')); el.classList.add('selected');")
    html = html.replace("const idx=Number(el.dataset.idx), opt=Number(el.dataset.opt); answers[idx]=opt;", "const idx=Number(el.dataset.idx), opt=Number(el.dataset.opt);")
    html = html.replace("const score = Math.round(((c*1)-(w*neg))*100)/100;", "let score = Math.round(((c*1)-(w*neg))*100)/100; if(Object.is(score,-0)) score = 0;")
    return html


_old_send_private_results = base.send_private_results

async def send_private_results(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session:
        return
    chat_row = base.DBH.fetchone('SELECT username FROM known_chats WHERE chat_id=?', (session['chat_id'],))
    username = chat_row['username'] if chat_row else None
    ranking = base.get_session_ranking(session_id)
    rank_map = {int(r['user_id']): r for r in ranking}
    participants = base.DBH.fetchall('SELECT * FROM participants WHERE session_id=? AND eligible=1', (session_id,))
    total_users = max(1, len(ranking))
    for p in participants:
        user_id = int(p['user_id'])
        row = base.DBH.fetchone('SELECT started FROM known_users WHERE user_id=?', (user_id,))
        if not row or int(row['started'] or 0) != 1:
            continue
        if not await base.is_required_channel_member(context, user_id):
            continue
        rank_item = rank_map.get(user_id)
        if not rank_item:
            continue
        section_data = _section_breakdown_for_user(session_id, user_id)
        review_items = _user_review_items(session_id, user_id)
        buckets = {'correct': [], 'wrong': [], 'skipped': []}
        for item in review_items:
            q_no = item['q_no']
            qrow = base.DBH.fetchone('SELECT message_id FROM session_questions WHERE session_id=? AND q_no=?', (session_id, q_no))
            link = base.get_message_link(int(session['chat_id']), int(qrow['message_id'] or 0), username) if qrow else None
            label = f'<a href="{link}">Q{q_no}</a>' if link else f'Q{q_no}'
            buckets[item['status']].append(label)
        correct = int(rank_item['correct']); wrong = int(rank_item['wrong']); skipped = int(rank_item['skipped'])
        attempted = max(1, correct + wrong)
        accuracy = (correct / attempted) * 100.0
        percentage = (correct / max(1, int(session['total_questions']))) * 100.0
        percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
        duration_seconds = max(0, int((session['ended_at'] or base.now_ts()) - (session['started_at'] or base.now_ts())))
        lines = [f'<b>🏁 {base.html_escape(session["title"])}</b>', '', f'Rank: <b>#{rank_item["rank"]}</b> / {total_users}', f'Score: <b>{rank_item["score"]}</b>', f'Negative / wrong: <b>{session["negative_mark"]}</b>', f'✅ Correct: <b>{correct}</b>', f'❌ Wrong: <b>{wrong}</b>', f'➖ Skipped: <b>{skipped}</b>', f'🎯 Accuracy: <b>{accuracy:.2f}%</b>', f'📊 Percentage: <b>{percentage:.2f}%</b>', f'🏆 Percentile: <b>{percentile:.2f}</b>', f'⏱ Time: <b>{duration_seconds}s</b>', '']
        if section_data and (len(section_data) > 1 or section_data[0]['title'] != 'General'):
            lines.append('<b>Section Analysis</b>')
            for item in section_data:
                lines.append(f'• {base.html_escape(item["title"])} — ✅ {item["correct"]}  ❌ {item["wrong"]}  ➖ {item["skipped"]}')
            lines.append('')
        lines += ['<b>Correct</b>', ', '.join(buckets['correct']) or '—', '', '<b>Wrong</b>', ', '.join(buckets['wrong']) or '—', '', '<b>Skipped</b>', ', '.join(buckets['skipped']) or '—']
        buttons = []
        practice_url = _build_practice_url_v4(context.bot_data.get('bot_username', ''), str(session['draft_id']), int(session['created_by']))
        if practice_url:
            buttons.append([InlineKeyboardButton('🔁 Try Again', url=practice_url)])
        with suppress(TelegramError):
            await context.bot.send_message(user_id, '\n'.join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, disable_web_page_preview=True)


base.send_private_results = send_private_results


_old_begin_or_advance_exam = base.begin_or_advance_exam

async def begin_or_advance_exam(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session or session['status'] != 'running':
        return
    next_index = int(session['current_index'] or 0) + 1
    total = int(session['total_questions'] or 0)
    if next_index > total:
        await base.finish_exam(context, session_id, reason='completed')
        return
    q = base.get_session_question(session_id, next_index)
    if not q:
        await base.finish_exam(context, session_id, reason='missing_question')
        return
    options = base.jload(q['options'], []) or []
    section_title = base.normalize_visual_text(q['section_title'] or '')
    base_seconds = int(q['question_time_override'] or session['question_time'] or 30)
    speed_factor = float(session['speed_factor'] or 1.0)
    effective_seconds = max(5, int(round(base_seconds * speed_factor)))
    draft_row = base.get_draft(str(session['draft_id'])) if session['draft_id'] else None
    show_title = _draft_prefix_state(draft_row)
    q_text = _smart_clean_question_text(str(q['question'] or '')) or f'Question {next_index}'
    prefix_parts = [f'[{next_index}/{total}]']
    if section_title:
        prefix_parts.append(f'[{section_title}]')
    if show_title:
        prefix_parts.append(f'[{base.normalize_visual_text(session["title"])}]')
    question_prefix = (' '.join(prefix_parts) + '\n') if prefix_parts else ''
    poll_question = (question_prefix + q_text).strip() or q_text
    if len(poll_question) > 300:
        allowed_q = max(10, 300 - len(question_prefix))
        poll_question = question_prefix + q_text[:allowed_q - 1].rstrip() + '…'
    explanation_text = _smart_clean_explanation_text(str(q['explanation'] or f'Question {next_index} of {total}'))
    if len(explanation_text) > 200:
        explanation_text = explanation_text[:199] + '…'
    image_bytes = _render_math_question_image(q_text, [str(x) for x in options])
    try:
        if image_bytes:
            await context.bot.send_photo(chat_id=session['chat_id'], photo=InputFile(_io.BytesIO(image_bytes), filename=f'q_{next_index}.png'), caption=' '.join(prefix_parts) or f'Question {next_index}/{total}')
        msg = await context.bot.send_poll(chat_id=session['chat_id'], question=(poll_question if not image_bytes else (' '.join(prefix_parts) or f'Question {next_index}/{total}')), options=options, type=Poll.QUIZ, is_anonymous=False, allows_multiple_answers=False, correct_option_id=int(q['correct_option']), explanation=explanation_text, open_period=effective_seconds)
    except TelegramError as exc:
        base.logger.exception('Failed to send poll: %s', exc)
        await base.finish_exam(context, session_id, reason='send_poll_error')
        return
    poll_id = msg.poll.id
    with closing(base.DBH.connect()) as conn:
        conn.execute('UPDATE session_questions SET poll_id=?, message_id=?, open_ts=?, close_ts=? WHERE session_id=? AND q_no=?', (poll_id, msg.message_id, base.now_ts(), base.now_ts() + effective_seconds, session_id, next_index))
        conn.execute('UPDATE sessions SET current_index=?, active_poll_id=?, active_poll_message_id=? WHERE id=?', (next_index, poll_id, msg.message_id, session_id))
        conn.commit()
    context.job_queue.run_once(base.close_poll_job, when=max(1, effective_seconds), data={'session_id': session_id, 'q_no': next_index}, name=f'close:{session_id}:{next_index}')


base.begin_or_advance_exam = begin_or_advance_exam


_old_handle_inline_query = handle_inline_query

async def handle_inline_query(update: Update, context) -> None:
    iq = update.inline_query
    if not iq or not iq.from_user:
        return
    user_id = iq.from_user.id
    if not base.user_has_staff_access(user_id):
        await iq.answer([], cache_time=0, is_personal=True)
        return
    query = base.normalize_visual_text(iq.query or '')
    drafts = base.list_user_drafts(user_id)
    filtered = []
    for row in drafts:
        q_count = int(row.get('q_count', 0) if isinstance(row, dict) else row['q_count'])
        if q_count <= 0:
            continue
        title = str(row['title'])
        code = str(row['id'])
        if not query or query.casefold() in code.casefold() or query.casefold() in title.casefold() or query.casefold() == f'quiz:{code.casefold()}':
            filtered.append(row)
    filtered = filtered[:20]
    bot_username = context.bot_data.get('bot_username', '')
    results = []
    for row in filtered:
        practice = base.ensure_practice_link(str(row['id']), int(row['owner_id']))
        practice_url = f'https://t.me/{bot_username}?start=practice_{practice["token"]}' if bot_username else ''
        prefix = 'ON' if _draft_prefix_state(row) else 'OFF'
        text = f"<b>{base.html_escape(row['title'])}</b>\nQuiz ID: <code>{row['id']}</code>\nQuestions: <b>{row['q_count']}</b>\nTime / question: <b>{row['question_time']} sec</b>\nNegative / wrong: <b>{row['negative_mark']}</b>\nPrefix: <b>{prefix}</b>"
        if practice_url:
            text += f"\n\nPractice link:\n{practice_url}"
        results.append(InlineQueryResultArticle(id=str(row['id']), title=f"{row['title']} [{row['id']}]", description=f"{row['q_count']} questions • {row['question_time']}s • -{row['negative_mark']} • Prefix {prefix}", input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML)))
    await iq.answer(results, cache_time=0, is_personal=True)


_prev_callback_router_v5 = base.callback_router

async def callback_router(update: Update, context) -> None:
    query = update.callback_query
    if not query or not query.data:
        return await _prev_callback_router_v5(update, context)
    data = query.data
    user = query.from_user
    if user:
        base.record_user(user)
    if data.startswith('uxq:'):
        await query.answer()
        if not user or not base.user_has_staff_access(user.id):
            return
        parts = data.split(':')
        action = parts[1] if len(parts) > 1 else ''
        if action == 'browse' and len(parts) >= 4:
            draft_id, page = parts[2], _safe_int(parts[3])
            text, kb = _build_question_manager_text_markup(user.id, draft_id, page)
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
        if action == 'add' and len(parts) >= 5:
            draft_id, q_no, page = parts[2], _safe_int(parts[3]), _safe_int(parts[4])
            draft = resolve_editable_draft(user.id, draft_id)
            if not draft:
                text, kb = _build_draft_browser_list_text_markup(user.id, header='⚠️ Draft not found.')
                await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
                return
            base.set_user_state(user.id, 'adv2_add_questions', {'draft_id': draft_id, 'page': page, 'insert_after': q_no})
            anchor = 'the start' if q_no <= 0 else ('the end' if q_no >= _calc_total_draft_questions(draft_id) else f'Q{q_no}')
            await _show_prompt(context, user.id, f"<b>{base.html_escape(draft['title'])}</b>\nCode: <code>{draft_id}</code>\n\nSend MCQ text now. New questions will be inserted after <b>{anchor}</b>.\nYou can also use <code>@3</code> on the first line to insert after Q3.")
            return
        if action == 'del' and len(parts) >= 5:
            draft_id, q_no, page = parts[2], _safe_int(parts[3]), _safe_int(parts[4])
            removed = delete_single_question(draft_id, q_no)
            sanitize_existing_draft_questions(draft_id)
            text, kb = _build_question_manager_text_markup(user.id, draft_id, page, f'✅ Removed <b>{removed}</b> question(s).')
            await base.panel_show_message(query.message, user.id, text, reply_markup=kb)
            return
    return await _prev_callback_router_v5(update, context)


base.callback_router = callback_router


_prev_handle_text_v6 = base.handle_text

async def handle_text(update: Update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not getattr(message, 'text', None):
        return await _prev_handle_text_v6(update, context)
    state, payload = base.get_user_state(user.id)
    cmd, args = base.extract_command(message.text, context.bot_data.get('bot_username', ''))
    cmd = (cmd or '').lower()
    if chat.type == 'private' and state == 'adv2_add_questions' and not cmd:
        draft_id = str(payload.get('draft_id') or '')
        page = int(payload.get('page') or 0)
        draft = resolve_editable_draft(user.id, draft_id)
        base.clear_user_state(user.id)
        if not draft:
            await _show_draft_browser(context, user.id, page=page, header='⚠️ Draft not found or access denied.')
            return
        insert_after = payload.get('insert_after')
        parsed_insert, body = _extract_insert_position_from_text(message.text.strip())
        if parsed_insert is not None:
            insert_after = parsed_insert
        parsed = parse_marked_questions_from_text(body)
        if not parsed:
            text, kb = _build_question_manager_text_markup(user.id, draft_id, page, '⚠️ No valid questions were found in the text you sent.')
            await base.panel_show_message(message, user.id, text, reply_markup=kb)
            return
        clean_items = []
        for item in parsed:
            q = _smart_clean_question_text(item['question'])
            opts, new_correct = _clean_and_map_options(item['options'], int(item['correct_option']))
            if q and len(opts) >= 2 and new_correct is not None:
                clean_items.append({'question': q, 'options': opts, 'correct_option': int(new_correct), 'explanation': _smart_clean_explanation_text(item.get('explanation') or ''), 'src': 'text_manual'})
        added = insert_questions_into_draft(draft_id, clean_items, insert_after)
        with suppress(Exception):
            await base.safe_delete_message(context.bot, chat.id, message.message_id)
        where = 'start' if _safe_int(insert_after) <= 0 else ('end' if insert_after is None else f'after Q{insert_after}')
        text, kb = _build_question_manager_text_markup(user.id, draft_id, page, f'✅ Draft updated. Added <b>{added}</b> question(s) at <b>{where}</b>.')
        await base.panel_show_message(message, user.id, text, reply_markup=kb)
        return
    return await _prev_handle_text_v6(update, context)


base.handle_text = handle_text


# ============================================================
# Final UX patch v7: professional responsive HTML export,
# offline-safe latex display, improved math image rendering,
# cleaner result delivery, fixed prefix runtime behavior
# ============================================================

import base64 as _base64
from PIL import Image as _Image, ImageDraw as _ImageDraw

_LATEX_TOKEN_MAP = {
    r'\\displaystyle': '',
    r'\\textstyle': '',
    r'\\cdot': '·',
    r'\\times': '×',
    r'\\div': '÷',
    r'\\pm': '±',
    r'\\mp': '∓',
    r'\\neq': '≠',
    r'\\ne': '≠',
    r'\\leq': '≤',
    r'\\geq': '≥',
    r'\\le': '≤',
    r'\\ge': '≥',
    r'\\infty': '∞',
    r'\\pi': 'π',
    r'\\theta': 'θ',
    r'\\alpha': 'α',
    r'\\beta': 'β',
    r'\\gamma': 'γ',
    r'\\delta': 'δ',
    r'\\Delta': 'Δ',
    r'\\lambda': 'λ',
    r'\\mu': 'μ',
    r'\\sigma': 'σ',
    r'\\omega': 'ω',
    r'\\Omega': 'Ω',
    r'\\phi': 'φ',
    r'\\Phi': 'Φ',
    r'\\sum': '∑',
    r'\\prod': '∏',
    r'\\int': '∫',
    r'\\lim': 'lim',
    r'\\sin': 'sin',
    r'\\cos': 'cos',
    r'\\tan': 'tan',
    r'\\cot': 'cot',
    r'\\sec': 'sec',
    r'\\csc': 'csc',
    r'\\ln': 'ln',
    r'\\log': 'log',
    r'\\cup': '∪',
    r'\\cap': '∩',
    r'\\subseteq': '⊆',
    r'\\subset': '⊂',
    r'\\supseteq': '⊇',
    r'\\supset': '⊃',
    r'\\Rightarrow': '⇒',
    r'\\rightarrow': '→',
    r'\\leftarrow': '←',
    r'\\to': '→',
    r'\\approx': '≈',
    r'\\therefore': '∴',
    r'\\because': '∵',
    r'\\degree': '°',
}

_SUPERSCRIPT_MAP = str.maketrans({
    '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
    '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾', 'n': 'ⁿ', 'i': 'ⁱ'
})


def _latex_to_pretty_text(raw: str) -> str:
    text = str(raw or '')
    if not text:
        return ''
    text = text.replace('\r', '')
    text = text.replace('\\(', '').replace('\\)', '')
    text = text.replace('\\[', '').replace('\\]', '')
    for k, v in _LATEX_TOKEN_MAP.items():
        text = text.replace(k, v)
    frac_re = re.compile(r'\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}')
    sqrt_re = re.compile(r'\\sqrt\s*\{([^{}]+)\}')
    while frac_re.search(text):
        text = frac_re.sub(lambda m: f"({m.group(1)})/({m.group(2)})", text)
    while sqrt_re.search(text):
        text = sqrt_re.sub(lambda m: f"√({m.group(1)})", text)
    text = re.sub(r'\^\{([^{}]+)\}', lambda m: m.group(1).translate(_SUPERSCRIPT_MAP), text)
    text = re.sub(r'\^([A-Za-z0-9+\-=()])', lambda m: m.group(1).translate(_SUPERSCRIPT_MAP), text)
    text = re.sub(r'_(\{([^{}]+)\}|([A-Za-z0-9+\-=()]))', lambda m: '_' + (m.group(2) or m.group(3) or ''), text)
    text = re.sub(r'\\mathrm\{([^{}]+)\}', lambda m: m.group(1), text)
    text = re.sub(r'\\text\{([^{}]+)\}', lambda m: m.group(1), text)
    text = re.sub(r'\\operatorname\{([^{}]+)\}', lambda m: m.group(1), text)
    text = re.sub(r'\\([A-Za-z]+)', lambda m: m.group(1), text)
    text = text.replace('{', '').replace('}', '')
    text = text.replace('$$', '')
    text = text.replace('$', '')
    text = text.replace('\\', '')
    text = text.replace('  ', ' ')
    return base.normalize_visual_text(text).strip()


def _html_from_display_text(raw: str) -> str:
    return base.html_escape(_latex_to_pretty_text(raw)).replace('\n', '<br>')


def _png_data_uri(blob: Optional[bytes]) -> str:
    if not blob:
        return ''
    return 'data:image/png;base64,' + _base64.b64encode(blob).decode('ascii')


def _render_readable_text_image(text: str, width: int = 1280, font_size: int = 34, padding: int = 42, bg: str = '#ffffff', fg: str = '#101827', title: str = '') -> Optional[bytes]:
    text = _latex_to_pretty_text(text)
    if not text:
        return None
    try:
        probe = _Image.new('RGB', (width, 200), bg)
        probe_draw = _ImageDraw.Draw(probe)
        title_font = base.FONTS.get('bold', max(28, font_size + 6))
        body_font = base.FONTS.get('regular', font_size)
        y = padding
        total_h = padding
        if title:
            _, title_bottom = base.draw_multiline(probe_draw, title, (padding, y), title_font, fg, width - 2 * padding, line_gap=6)
            total_h = max(total_h, title_bottom)
            y = title_bottom + 12
        lines = base.wrap_text(probe_draw, text, body_font, width - 2 * padding)
        line_h = max(28, font_size + 10)
        total_h = y + (len(lines) * line_h) + padding
        img = _Image.new('RGB', (width, total_h), bg)
        draw = _ImageDraw.Draw(img)
        y = padding
        if title:
            _, title_bottom = base.draw_multiline(draw, title, (padding, y), title_font, fg, width - 2 * padding, line_gap=6)
            y = title_bottom + 12
        for line in lines:
            draw.text((padding, y), line, font=body_font, fill=fg)
            y += line_h
        out = _io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _render_poll_question_image(question: str, options: List[str]) -> Optional[bytes]:
    needs_image = _contains_math_markup(question) or any(_contains_math_markup(x) for x in options)
    if not needs_image:
        return None
    pretty_q = _latex_to_pretty_text(question)
    pretty_opts = [_latex_to_pretty_text(x) for x in options]
    try:
        width = 1500
        padding = 54
        bg = '#ffffff'
        fg = '#101827'
        probe = _Image.new('RGB', (width, 100), bg)
        pd = _ImageDraw.Draw(probe)
        title_font = base.FONTS.get('bold', 40)
        q_font = base.FONTS.get('regular', 38)
        opt_font = base.FONTS.get('regular', 32)
        q_lines = base.wrap_text(pd, pretty_q, q_font, width - 2 * padding)
        total_h = padding + 56 + (len(q_lines) * 52) + 20
        for idx, opt in enumerate(pretty_opts):
            opt_lines = base.wrap_text(pd, f"{chr(65+idx)}. {opt}", opt_font, width - 2 * padding - 20)
            total_h += max(52, len(opt_lines) * 42) + 18
        total_h += padding
        img = _Image.new('RGB', (width, total_h), bg)
        draw = _ImageDraw.Draw(img)
        y = padding
        draw.rounded_rectangle((24, 24, width - 24, total_h - 24), radius=28, outline='#d0d7e2', width=2, fill='#ffffff')
        draw.text((padding, y), 'Math / Latex Question', font=title_font, fill='#1d4ed8')
        y += 64
        for line in q_lines:
            draw.text((padding, y), line, font=q_font, fill=fg)
            y += 52
        y += 12
        for idx, opt in enumerate(pretty_opts):
            label = f"{chr(65+idx)}. {opt}"
            opt_lines = base.wrap_text(draw, label, opt_font, width - 2 * padding - 20)
            box_h = max(52, len(opt_lines) * 42 + 16)
            draw.rounded_rectangle((padding - 10, y - 8, width - padding + 10, y + box_h - 8), radius=22, outline='#d8dee9', width=2, fill='#fbfbfd')
            yy = y + 4
            for line in opt_lines:
                draw.text((padding + 10, yy), line, font=opt_font, fill=fg)
                yy += 42
            y += box_h + 16
        out = _io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _display_option_text(option: str, idx: int) -> str:
    display = _latex_to_pretty_text(option)
    return display or chr(65 + idx)


def _export_theme_palette(owner_id: int, draft: Any) -> Dict[str, str]:
    creator = _current_creator_theme(owner_id)
    accent = creator.get('accent', '#2563eb')
    bg = creator.get('bg', '#081120')
    text = creator.get('text', '#eaf2ff')
    muted = creator.get('muted', '#94a3b8')
    return {
        'accent': accent,
        'accent_soft': 'rgba(37,99,235,.14)',
        'danger': '#dc2626',
        'success': '#16a34a',
        'warning': '#d97706',
        'light_bg': '#f7f8fb',
        'light_text': '#0f172a',
        'light_card': '#ffffff',
        'light_border': '#e2e8f0',
        'light_muted': '#64748b',
        'dark_bg': '#05070b',
        'dark_surface': '#081120',
        'dark_surface_2': '#0d1728',
        'dark_text': text,
        'dark_muted': muted,
        'dark_border': 'rgba(148,163,184,.18)',
        'default_mode': 'dark' if str(draft['html_export_theme'] or 'dark').lower() == 'dark' else 'light',
        'deep_bg': bg,
    }


def render_scroll_exam_html(draft: Any, owner_id: int) -> str:
    theme = _export_theme_palette(owner_id, draft)
    questions = _draft_question_rows_with_sections(str(draft['id']))
    if not questions:
        raise ValueError('Draft has no valid questions.')
    sections = sorted({q['section'] for q in questions})
    export_rows = []
    for q in questions:
        q_raw = str(q['question'] or '')
        q_image = _render_readable_text_image(q_raw, width=1300, font_size=36, title=f"Question {q['q_no']}") if _contains_math_markup(q_raw) else None
        export_rows.append({
            'q_no': int(q['q_no']),
            'section': q['section'],
            'question_text': _html_from_display_text(q_raw),
            'question_image': _png_data_uri(q_image),
            'options': [_html_from_display_text(_display_option_text(opt, idx)) for idx, opt in enumerate(q['options'])],
            'correct': int(q['correct_option']),
            'explanation': _html_from_display_text(q['explanation']),
        })
    tpl = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<style>
:root{
  --accent: __ACCENT__;
  --accent-soft: __ACCENT_SOFT__;
  --danger: __DANGER__;
  --success: __SUCCESS__;
  --warning: __WARNING__;
  --radius: 22px;
  --shadow: 0 18px 48px rgba(15,23,42,.12);
}
*{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,"Noto Sans Bengali",sans-serif;background:var(--page-bg);color:var(--text);transition:.22s ease background,.22s ease color}
body[data-theme="light"]{--page-bg:__LIGHT_BG__;--text:__LIGHT_TEXT__;--muted:__LIGHT_MUTED__;--surface:__LIGHT_CARD__;--surface-2:#f8fafc;--border:__LIGHT_BORDER__;--hero:#ffffff;--glass:rgba(255,255,255,.92);--chip:rgba(37,99,235,.08)}
body[data-theme="dark"]{--page-bg:__DARK_BG__;--text:__DARK_TEXT__;--muted:__DARK_MUTED__;--surface:__DARK_SURFACE__;--surface-2:__DARK_SURFACE_2__;--border:__DARK_BORDER__;--hero:linear-gradient(135deg,__DEEP_BG__,#0d1b32);--glass:rgba(8,17,32,.94);--chip:rgba(148,163,184,.12)}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
.page{display:none;min-height:100vh}.page.active{display:block}
.shell{width:min(1100px,100% - 28px);margin-inline:auto}
.top-fixed{position:fixed;top:0;left:0;right:0;z-index:200;background:var(--glass);backdrop-filter:blur(18px);border-bottom:1px solid var(--border)}
.top-fixed .inner{width:min(1100px,100% - 28px);margin-inline:auto;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 0}
.brand{display:flex;flex-direction:column;gap:4px}.brand h1{margin:0;font-size:clamp(20px,3vw,30px);line-height:1.1}.brand .meta{font-size:clamp(12px,2vw,15px);color:var(--muted)}
.timer-box{min-width:126px;padding:14px 18px;border-radius:18px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);text-align:center}.timer-box .label{font-size:12px;color:var(--muted)}.timer-box .value{font-size:clamp(22px,4vw,30px);font-weight:900}
.hero{padding:40px 0 28px}.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}
.start-card{padding:28px;display:grid;gap:18px;background:var(--hero)}.headline{font-size:clamp(28px,5vw,44px);font-weight:900;line-height:1.08}
.muted{color:var(--muted)} .submeta{display:flex;flex-wrap:wrap;gap:10px 16px;font-size:clamp(14px,2vw,18px)}
.input{width:100%;padding:16px 18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-size:16px;outline:none}
.input:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(37,99,235,.12)}
.chips{display:flex;flex-wrap:wrap;gap:12px}.chip{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:16px;background:var(--chip);border:1px solid var(--border);font-weight:700}.chip input{accent-color:var(--accent)}
.actions{display:flex;flex-wrap:wrap;gap:12px}.btn{border:0;border-radius:18px;padding:15px 20px;font-weight:800;font-size:16px;cursor:pointer;transition:.18s ease transform,.18s ease filter}.btn:hover{transform:translateY(-1px)}.btn.primary{background:var(--accent);color:#fff}.btn.secondary{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.exam-wrap{padding-top:108px;padding-bottom:104px}.exam-grid{display:grid;gap:18px}.question-card{padding:22px;scroll-margin-top:104px;background:linear-gradient(180deg,var(--surface),var(--surface-2))}
.q-top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:14px}.q-index{font-size:clamp(24px,4vw,36px);font-weight:900;color:#d9f7be}.q-section{padding:8px 14px;border-radius:999px;background:var(--chip);border:1px solid var(--border);font-weight:700}
.q-text{font-size:clamp(20px,3vw,34px);line-height:1.55;font-weight:700;word-break:break-word}.q-text.small{font-size:clamp(18px,2.8vw,28px)}
.q-image{border-radius:18px;border:1px solid var(--border);margin:14px 0;background:#fff;overflow:hidden}
.options{display:grid;gap:14px;margin-top:20px}.opt{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:flex-start;padding:16px 18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2);cursor:pointer;transition:.18s ease border-color,.18s ease transform,.18s ease background}.opt:hover{transform:translateY(-1px)}.opt.selected{border-color:var(--accent);background:var(--accent-soft)}.opt.locked{cursor:not-allowed}.opt input{margin-top:4px;width:20px;height:20px;accent-color:var(--accent)}.opt-body{font-size:clamp(18px,2.8vw,26px);line-height:1.48;word-break:break-word}.opt-label{font-weight:900;margin-right:10px}
.float-actions{position:fixed;right:16px;bottom:16px;display:flex;gap:12px;z-index:220}.fab{min-width:58px;height:58px;padding:0 18px;border-radius:999px;border:1px solid var(--border);background:var(--glass);backdrop-filter:blur(12px);color:var(--text);font-weight:900;box-shadow:var(--shadow);cursor:pointer}.fab.primary{background:var(--accent);color:#fff;border-color:transparent}
.drawer-backdrop{position:fixed;inset:0;background:rgba(2,6,23,.32);opacity:0;pointer-events:none;transition:.2s ease;z-index:250}.drawer-backdrop.show{opacity:1;pointer-events:auto}.drawer{position:fixed;left:0;right:0;bottom:0;transform:translateY(105%);transition:.22s ease;z-index:260;background:var(--surface);border-top-left-radius:28px;border-top-right-radius:28px;border:1px solid var(--border);padding:22px;max-height:78vh;overflow:auto;box-shadow:0 -18px 48px rgba(2,6,23,.16)}.drawer.show{transform:translateY(0)}.drawer h3{margin:0 0 14px 0}.palette{display:grid;grid-template-columns:repeat(auto-fill,minmax(52px,1fr));gap:12px}.bubble{height:52px;border-radius:16px;border:1px solid var(--border);display:grid;place-items:center;font-weight:900;background:var(--surface-2);cursor:pointer}.bubble.answered{background:rgba(22,163,74,.16);border-color:rgba(22,163,74,.46)}.bubble.current{outline:3px solid var(--accent)}
.result-wrap{padding-top:28px;padding-bottom:28px}.hero-result{padding:26px}.score-ring{font-size:clamp(52px,10vw,96px);font-weight:900;line-height:1;color:var(--accent)}.result-title{font-size:clamp(28px,4vw,40px);font-weight:900}.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-top:18px}.stat{padding:18px;border-radius:20px;background:var(--surface-2);border:1px solid var(--border)}.stat .label{font-size:13px;color:var(--muted);font-weight:700}.stat .value{font-size:clamp(30px,4vw,42px);font-weight:900;margin-top:8px}
.section-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.bar{height:10px;border-radius:999px;background:rgba(148,163,184,.18);overflow:hidden;margin-top:10px}.bar > span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),#ef4444)}
.tabs{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.tab{padding:12px 16px;border-radius:16px;border:1px solid var(--border);background:var(--surface-2);font-weight:800;cursor:pointer}.tab.active{background:var(--accent);color:#fff;border-color:transparent}.review-list{display:grid;gap:16px}.review-card{padding:18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2)}.review-card.correct{border-left:5px solid var(--success)}.review-card.wrong{border-left:5px solid var(--danger)}.review-card.skipped{border-left:5px solid var(--warning)}.review-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}.review-q{font-size:clamp(18px,2.8vw,24px);font-weight:800;line-height:1.5}.answer-line{font-size:16px;line-height:1.55;margin-top:8px}
@media (max-width: 720px){.top-fixed .inner{padding:12px 0}.timer-box{min-width:108px;padding:10px 12px}.question-card{padding:18px}.float-actions{right:12px;bottom:12px}.fab{height:54px}}
</style>
</head>
<body data-theme="__DEFAULT_MODE__">
<div id="startPage" class="page active">
  <div class="hero shell">
    <div class="start-card card">
      <div class="headline">__TITLE__</div>
      <div class="submeta muted">Questions: __QCOUNT__ • Time / question: __QTIME__ sec • Negative: __NEG__</div>
      <input id="studentName" class="input" type="text" placeholder="Enter your name">
      <div>
        <div style="font-size:22px;font-weight:900;margin-bottom:10px">Select sections</div>
        <div id="sectionBox" class="chips"></div>
      </div>
      <div class="actions">
        <button id="startBtn" class="btn primary">Start HTML Exam</button>
        <button id="toggleThemeBtn" class="btn secondary">Toggle Theme</button>
      </div>
    </div>
  </div>
</div>
<div id="examPage" class="page">
  <div class="top-fixed">
    <div class="inner">
      <div class="brand"><h1>__TITLE__</h1><div id="metaLine" class="meta">Loading exam…</div></div>
      <div class="timer-box"><div class="label">Remaining</div><div id="timerValue" class="value">00:00</div></div>
    </div>
  </div>
  <div class="exam-wrap shell">
    <div id="questionList" class="exam-grid"></div>
  </div>
  <div class="float-actions">
    <button id="jumpBtn" class="fab">Sections</button>
    <button id="submitBtn" class="fab primary">Submit</button>
  </div>
  <div id="drawerBackdrop" class="drawer-backdrop"></div>
  <div id="drawer" class="drawer">
    <h3>Sections & Question List</h3>
    <div id="sectionJump" class="chips" style="margin-bottom:16px"></div>
    <div id="palette" class="palette"></div>
  </div>
</div>
<div id="resultPage" class="page">
  <div class="result-wrap shell">
    <div class="hero-result card">
      <div class="muted">__TITLE__</div>
      <div id="resultName" class="result-title">Result</div>
      <div id="resultScore" class="score-ring">0.00</div>
      <div class="muted">Professional performance report</div>
      <div id="summaryGrid" class="summary-grid"></div>
    </div>
    <div class="card" style="padding:22px;margin-top:16px">
      <div style="font-size:24px;font-weight:900;margin-bottom:12px">Section Analysis</div>
      <div id="sectionResultGrid" class="section-grid"></div>
    </div>
    <div class="card" style="padding:22px;margin-top:16px">
      <div style="font-size:24px;font-weight:900">Answer Review</div>
      <div class="tabs">
        <button class="tab active" data-filter="all">All</button>
        <button class="tab" data-filter="correct">Correct</button>
        <button class="tab" data-filter="wrong">Wrong</button>
        <button class="tab" data-filter="skipped">Skipped</button>
      </div>
      <div id="reviewList" class="review-list"></div>
    </div>
  </div>
</div>
<script>
const QUESTIONS = __DATA__;
const SECTIONS = __SECTIONS__;
const NEGATIVE_MARK = __NEG_FLOAT__;
const QUESTION_TIME = __QTIME__;
let selectedSections = new Set(SECTIONS);
let active = [];
let answers = {};
let totalSeconds = 0;
let leftSeconds = 0;
let timer = null;
let filterMode = 'all';
let currentQuestion = 0;
const $ = (id) => document.getElementById(id);
function fmt(sec){sec=Math.max(0,Math.floor(sec));const m=Math.floor(sec/60);const s=sec%60;return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');}
function toggleTheme(){document.body.setAttribute('data-theme', document.body.getAttribute('data-theme')==='dark'?'light':'dark');}
$('toggleThemeBtn').onclick = toggleTheme;
function buildSectionSelectors(){
  const box = $('sectionBox'); box.innerHTML='';
  SECTIONS.forEach(sec=>{
    const label = document.createElement('label'); label.className='chip';
    label.innerHTML = `<input type="checkbox" checked> <span>${sec}</span>`;
    const input = label.querySelector('input');
    input.onchange = ()=>{ if(input.checked) selectedSections.add(sec); else selectedSections.delete(sec); };
    box.appendChild(label);
  });
}
function showPage(id){ ['startPage','examPage','resultPage'].forEach(x=>$(x).classList.remove('active')); $(id).classList.add('active'); }
function lockQuestion(idx, opt){
  if(answers[idx] !== undefined) return;
  answers[idx] = opt;
  document.querySelectorAll(`.opt[data-idx="${idx}"]`).forEach(el=>{ el.classList.add('locked'); const radio=el.querySelector('input'); if(radio) radio.disabled=true; });
  const selected = document.querySelector(`.opt[data-idx="${idx}"][data-opt="${opt}"]`); if(selected) selected.classList.add('selected');
  updatePalette();
  currentQuestion = idx;
  const next = document.getElementById(`q-${idx+1}`);
  if(next){ setTimeout(()=>next.scrollIntoView({behavior:'smooth', block:'start'}), 160); }
}
function renderExam(){
  active = QUESTIONS.filter(q=>selectedSections.has(q.section));
  if(!active.length) active = [...QUESTIONS];
  answers = {}; currentQuestion = 0;
  totalSeconds = active.length * QUESTION_TIME; leftSeconds = totalSeconds;
  $('metaLine').textContent = `${active.length} questions • sections: ${[...new Set(active.map(q=>q.section))].join(', ')}`;
  const list = $('questionList'); list.innerHTML='';
  active.forEach((q, idx)=>{
    const card = document.createElement('div'); card.className='question-card card'; card.id = `q-${idx}`;
    const options = q.options.map((opt,i)=>`<label class="opt" data-idx="${idx}" data-opt="${i}"><input type="radio" name="q_${idx}"><div class="opt-body"><span class="opt-label">${String.fromCharCode(65+i)}.</span>${opt}</div></label>`).join('');
    const qBlock = q.question_image ? `<div class="q-image"><img src="${q.question_image}" alt="Question ${idx+1}"></div>` : `<div class="q-text ${q.question_text.length>170?'small':''}">${q.question_text}</div>`;
    card.innerHTML = `<div class="q-top"><div><span class="q-index">[${idx+1}/${active.length}]</span><span class="q-section">${q.section}</span></div></div>${qBlock}<div class="options">${options}</div>`;
    list.appendChild(card);
  });
  document.querySelectorAll('.opt').forEach(el=>{
    el.addEventListener('click', ()=>{
      const idx = Number(el.dataset.idx), opt = Number(el.dataset.opt);
      lockQuestion(idx, opt);
    });
  });
  buildDrawer();
  updatePalette();
}
function buildDrawer(){
  const jump = $('sectionJump'); jump.innerHTML='';
  [...new Set(active.map(q=>q.section))].forEach(sec=>{
    const btn = document.createElement('button'); btn.className='btn secondary'; btn.textContent = sec;
    btn.onclick = ()=>{ const row = active.findIndex(x=>x.section===sec); if(row>=0){ currentQuestion=row; document.getElementById(`q-${row}`).scrollIntoView({behavior:'smooth', block:'start'}); closeDrawer(); updatePalette(); } };
    jump.appendChild(btn);
  });
  const palette = $('palette'); palette.innerHTML='';
  active.forEach((q, idx)=>{
    const bubble = document.createElement('button'); bubble.className='bubble'; bubble.textContent = idx+1;
    bubble.onclick = ()=>{ currentQuestion=idx; document.getElementById(`q-${idx}`).scrollIntoView({behavior:'smooth', block:'start'}); closeDrawer(); updatePalette(); };
    if(answers[idx]!==undefined) bubble.classList.add('answered');
    if(idx===currentQuestion) bubble.classList.add('current');
    palette.appendChild(bubble);
  });
}
function updatePalette(){
  buildDrawer();
}
function openDrawer(){ $('drawerBackdrop').classList.add('show'); $('drawer').classList.add('show'); }
function closeDrawer(){ $('drawerBackdrop').classList.remove('show'); $('drawer').classList.remove('show'); }
$('jumpBtn').onclick = openDrawer; $('drawerBackdrop').onclick = closeDrawer;
function startTimer(){ clearInterval(timer); $('timerValue').textContent = fmt(leftSeconds); timer = setInterval(()=>{ leftSeconds -= 1; $('timerValue').textContent = fmt(leftSeconds); if(leftSeconds<=0){ clearInterval(timer); finishExam(); } }, 1000); }
function buildSummaryCards(summary){ $('summaryGrid').innerHTML = summary.map(item=>`<div class="stat"><div class="label">${item.label}</div><div class="value">${item.value}</div></div>`).join(''); }
function finishExam(){
  clearInterval(timer);
  let correct=0, wrong=0, skipped=0;
  active.forEach((q, idx)=>{ if(answers[idx]===undefined) skipped++; else if(Number(answers[idx])===Number(q.correct)) correct++; else wrong++; });
  let score = Math.round(((correct*1) - (wrong*NEGATIVE_MARK))*100)/100; if(Object.is(score,-0)) score = 0;
  const attempted = Math.max(1, correct + wrong);
  const accuracy = ((correct / attempted) * 100).toFixed(2) + '%';
  const percentage = ((correct / Math.max(1, active.length)) * 100).toFixed(2) + '%';
  const usedSeconds = totalSeconds - leftSeconds;
  $('resultName').textContent = `${$('studentName').value.trim() || 'Student'} — ${'__TITLE_TEXT__'}`;
  $('resultScore').textContent = score.toFixed(2);
  buildSummaryCards([
    {label:'Correct', value:correct},
    {label:'Wrong', value:wrong},
    {label:'Skipped', value:skipped},
    {label:'Negative / wrong', value:NEGATIVE_MARK.toFixed(2)},
    {label:'Accuracy', value:accuracy},
    {label:'Percentage', value:percentage},
    {label:'Time used', value:fmt(usedSeconds)},
    {label:'Questions', value:active.length}
  ]);
  const sectionMap = {};
  active.forEach((q, idx)=>{
    if(!sectionMap[q.section]) sectionMap[q.section] = {total:0, correct:0, wrong:0, skipped:0};
    sectionMap[q.section].total += 1;
    if(answers[idx]===undefined) sectionMap[q.section].skipped += 1;
    else if(Number(answers[idx])===Number(q.correct)) sectionMap[q.section].correct += 1;
    else sectionMap[q.section].wrong += 1;
  });
  $('sectionResultGrid').innerHTML = Object.entries(sectionMap).map(([name, item])=>{
    const pct = item.total ? Math.round((item.correct/item.total)*100) : 0;
    return `<div class="stat"><div class="label">${name}</div><div class="value">${item.correct}/${item.total}</div><div class="muted">Wrong ${item.wrong} • Skipped ${item.skipped}</div><div class="bar"><span style="width:${pct}%"></span></div></div>`;
  }).join('');
  const review = active.map((q, idx)=>{
    const ans = answers[idx];
    const status = ans===undefined ? 'skipped' : (Number(ans)===Number(q.correct) ? 'correct' : 'wrong');
    const chosen = ans===undefined ? 'Skipped' : q.options[ans];
    const qBlock = q.question_image ? `<div class="q-image" style="margin-bottom:10px"><img src="${q.question_image}" alt="Question ${idx+1}"></div>` : `<div class="review-q">${q.question_text}</div>`;
    return `<div class="review-card ${status}" data-status="${status}"><div class="review-head"><div><b>Q${idx+1}</b> • ${q.section}</div><div class="muted">${status.toUpperCase()}</div></div>${qBlock}<div class="answer-line"><b>Your answer:</b> ${chosen}</div><div class="answer-line"><b>Correct answer:</b> ${q.options[q.correct]}</div>${q.explanation ? `<div class="answer-line muted"><b>Explanation:</b> ${q.explanation}</div>` : ''}</div>`;
  }).join('');
  $('reviewList').innerHTML = review;
  showPage('resultPage');
  window.scrollTo({top:0, behavior:'smooth'});
}
function applyFilter(mode){
  filterMode = mode;
  document.querySelectorAll('.tab').forEach(btn=>btn.classList.toggle('active', btn.dataset.filter===mode));
  document.querySelectorAll('.review-card').forEach(card=>{ card.style.display = (mode==='all' || card.dataset.status===mode) ? '' : 'none'; });
}
document.querySelectorAll('.tab').forEach(btn=>btn.onclick = ()=>applyFilter(btn.dataset.filter));
$('startBtn').onclick = ()=>{ renderExam(); showPage('examPage'); window.scrollTo({top:0, behavior:'smooth'}); startTimer(); };
$('submitBtn').onclick = finishExam;
document.addEventListener('scroll', ()=>{
  const cards = [...document.querySelectorAll('.question-card')];
  let activeIdx = 0;
  for(const [idx, card] of cards.entries()){ const rect = card.getBoundingClientRect(); if(rect.top <= 120) activeIdx = idx; }
  currentQuestion = activeIdx; updatePalette();
}, {passive:true});
buildSectionSelectors();
</script>
</body>
</html>'''
    html = (tpl.replace('__TITLE__', title)
              .replace('__TITLE_TEXT__', base.normalize_visual_text(draft['title']))
              .replace('__QCOUNT__', str(len(questions)))
              .replace('__QTIME__', str(int(draft['question_time'])))
              .replace('__NEG__', str(draft['negative_mark']))
              .replace('__NEG_FLOAT__', str(float(draft['negative_mark'])))
              .replace('__DATA__', json.dumps(export_rows, ensure_ascii=False))
              .replace('__SECTIONS__', json.dumps(sections, ensure_ascii=False))
              .replace('__ACCENT__', theme['accent'])
              .replace('__ACCENT_SOFT__', theme['accent_soft'])
              .replace('__DANGER__', theme['danger'])
              .replace('__SUCCESS__', theme['success'])
              .replace('__WARNING__', theme['warning'])
              .replace('__LIGHT_BG__', theme['light_bg'])
              .replace('__LIGHT_TEXT__', theme['light_text'])
              .replace('__LIGHT_CARD__', theme['light_card'])
              .replace('__LIGHT_BORDER__', theme['light_border'])
              .replace('__LIGHT_MUTED__', theme['light_muted'])
              .replace('__DARK_BG__', theme['dark_bg'])
              .replace('__DARK_TEXT__', theme['dark_text'])
              .replace('__DARK_MUTED__', theme['dark_muted'])
              .replace('__DARK_SURFACE__', theme['dark_surface'])
              .replace('__DARK_SURFACE_2__', theme['dark_surface_2'])
              .replace('__DARK_BORDER__', theme['dark_border'])
              .replace('__DEEP_BG__', theme['deep_bg'])
              .replace('__DEFAULT_MODE__', theme['default_mode']))
    return html


def render_user_result_html(session: Any, participant_row: Any, rank_item: Dict[str, Any], ranking: List[Dict[str, Any]], review_items: List[Dict[str, Any]], section_items: List[Dict[str, Any]]) -> str:
    theme = _export_theme_palette(int(session['created_by']), {'html_export_theme': 'dark'})
    total_users = max(1, len(ranking))
    total_questions = int(session['total_questions'])
    correct = int(rank_item['correct'])
    wrong = int(rank_item['wrong'])
    skipped = int(rank_item['skipped'])
    attempted = max(1, correct + wrong)
    accuracy = (correct / attempted) * 100.0
    percentage = (correct / max(1, total_questions)) * 100.0
    percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
    score = str(rank_item['score'])
    top_rows = []
    for item in ranking[:15]:
        current = ' class="me"' if int(item['user_id']) == int(participant_row['user_id']) else ''
        display = base.html_escape(item['name'] + (f" {item['sub_name']}" if item.get('sub_name') else ''))
        top_rows.append(f"<tr{current}><td>{item['rank']}</td><td>{display}</td><td>{item['correct']}</td><td>{item['wrong']}</td><td>{item['skipped']}</td><td>{base.html_escape(str(item['score']))}</td></tr>")
    review_cards = []
    for item in review_items:
        status = item['status']
        q_html = _html_from_display_text(item['question'])
        exp_html = _html_from_display_text(item['explanation']) if item['explanation'] else ''
        review_cards.append(
            f"<div class='review-card {status}'><div class='head'><div><b>Q{item['q_no']}</b> • {base.html_escape(item['section'])}</div><div>{base.html_escape(status.title())}</div></div>"
            f"<div class='q'>{q_html}</div><div class='line'><b>Your answer:</b> {base.html_escape(_latex_to_pretty_text(item['chosen']))}</div>"
            f"<div class='line'><b>Correct answer:</b> {base.html_escape(_latex_to_pretty_text(item['correct']))}</div>"
            + (f"<div class='line muted'><b>Explanation:</b> {exp_html}</div>" if exp_html else '') + "</div>"
        )
    section_cards = []
    for sec in section_items:
        pct = 0 if not sec['total'] else round((sec['correct'] / sec['total']) * 100)
        section_cards.append(f"<div class='stat'><div class='label'>{base.html_escape(sec['title'])}</div><div class='value'>{sec['correct']}/{sec['total']}</div><div class='muted'>Wrong {sec['wrong']} • Skipped {sec['skipped']}</div><div class='bar'><span style='width:{pct}%'></span></div></div>")
    title = base.html_escape(base.normalize_visual_text(session['title']))
    name = base.html_escape(base.normalize_visual_text(participant_row['display_name'] or 'Student'))
    tpl = r'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>__TITLE__ — Result</title><style>
:root{--accent:__ACCENT__;--success:__SUCCESS__;--danger:__DANGER__;--warning:__WARNING__;--bg:__LIGHT_BG__;--text:__LIGHT_TEXT__;--muted:__LIGHT_MUTED__;--surface:__LIGHT_CARD__;--border:__LIGHT_BORDER__}body{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,Arial,"Noto Sans Bengali",sans-serif;background:var(--bg);color:var(--text)}.shell{width:min(1140px,100% - 28px);margin-inline:auto;padding:28px 0}.card{background:var(--surface);border:1px solid var(--border);border-radius:24px;box-shadow:0 18px 48px rgba(15,23,42,.12)}.hero{padding:26px}.title{font-size:clamp(28px,4vw,42px);font-weight:900}.name{color:var(--muted);margin-top:6px}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:18px}.stat{padding:18px;border-radius:18px;background:#f8fafc;border:1px solid var(--border)}.label{font-size:13px;color:var(--muted);font-weight:800}.value{font-size:clamp(30px,4vw,44px);font-weight:900;margin-top:8px}.two{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;margin-top:18px}.panel{padding:22px}.table{width:100%;border-collapse:separate;border-spacing:0 10px}.table th,.table td{padding:12px 14px;text-align:left}.table thead th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.table tbody tr{background:#f8fafc}.table tbody tr.me{outline:2px solid rgba(37,99,235,.18)}.table tbody td:first-child{border-top-left-radius:14px;border-bottom-left-radius:14px}.table tbody td:last-child{border-top-right-radius:14px;border-bottom-right-radius:14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.bar{height:10px;border-radius:999px;background:#e5e7eb;overflow:hidden;margin-top:10px}.bar span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),#0f172a)}.reviews{display:grid;gap:14px}.review-card{padding:18px;border-radius:18px;background:#f8fafc;border:1px solid var(--border)}.review-card.correct{border-left:5px solid var(--success)}.review-card.wrong{border-left:5px solid var(--danger)}.review-card.skipped{border-left:5px solid var(--warning)}.head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.q{font-size:18px;line-height:1.6;margin:10px 0}.line{margin-top:8px;line-height:1.5}.muted{color:var(--muted)}@media(max-width:900px){.two{grid-template-columns:1fr}}</style></head><body><div class="shell"><div class="card hero"><div class="title">__TITLE__</div><div class="name">Professional result report for __NAME__</div><div class="summary">__SUMMARY__</div></div><div class="two"><div class="card panel"><div class="title" style="font-size:24px">Ranking Board</div><table class="table"><thead><tr><th>#</th><th>Name</th><th>Correct</th><th>Wrong</th><th>Skipped</th><th>Score</th></tr></thead><tbody>__TOP_ROWS__</tbody></table></div><div class="card panel"><div class="title" style="font-size:24px">Section Analysis</div><div class="grid" style="margin-top:14px">__SECTION_CARDS__</div></div></div><div class="card panel" style="margin-top:18px"><div class="title" style="font-size:24px">Detailed Review</div><div class="reviews" style="margin-top:14px">__REVIEWS__</div></div></div></body></html>'''
    summary_html = ''.join([
        f"<div class='stat'><div class='label'>Rank</div><div class='value'>#{rank_item['rank']}/{total_users}</div></div>",
        f"<div class='stat'><div class='label'>Score</div><div class='value'>{score}</div></div>",
        f"<div class='stat'><div class='label'>Accuracy</div><div class='value'>{accuracy:.2f}%</div></div>",
        f"<div class='stat'><div class='label'>Percentage</div><div class='value'>{percentage:.2f}%</div></div>",
        f"<div class='stat'><div class='label'>Percentile</div><div class='value'>{percentile:.2f}</div></div>",
        f"<div class='stat'><div class='label'>Negative / wrong</div><div class='value'>{session['negative_mark']}</div></div>",
    ])
    return (tpl.replace('__TITLE__', title)
              .replace('__NAME__', name)
              .replace('__SUMMARY__', summary_html)
              .replace('__TOP_ROWS__', ''.join(top_rows))
              .replace('__SECTION_CARDS__', ''.join(section_cards))
              .replace('__REVIEWS__', ''.join(review_cards))
              .replace('__ACCENT__', theme['accent'])
              .replace('__SUCCESS__', theme['success'])
              .replace('__DANGER__', theme['danger'])
              .replace('__WARNING__', theme['warning'])
              .replace('__LIGHT_BG__', theme['light_bg'])
              .replace('__LIGHT_TEXT__', theme['light_text'])
              .replace('__LIGHT_MUTED__', theme['light_muted'])
              .replace('__LIGHT_CARD__', theme['light_card'])
              .replace('__LIGHT_BORDER__', theme['light_border']))


async def send_private_results(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session:
        return
    chat_row = base.DBH.fetchone('SELECT username FROM known_chats WHERE chat_id=?', (session['chat_id'],))
    username = chat_row['username'] if chat_row else None
    ranking = base.get_session_ranking(session_id)
    rank_map = {int(r['user_id']): r for r in ranking}
    participants = base.DBH.fetchall('SELECT * FROM participants WHERE session_id=? AND eligible=1', (session_id,))
    total_users = max(1, len(ranking))
    for p in participants:
        user_id = int(p['user_id'])
        row = base.DBH.fetchone('SELECT started FROM known_users WHERE user_id=?', (user_id,))
        if not row or int(row['started'] or 0) != 1:
            continue
        if not await base.is_required_channel_member(context, user_id):
            continue
        rank_item = rank_map.get(user_id)
        if not rank_item:
            continue
        review_items = _user_review_items(session_id, user_id)
        section_data = _section_breakdown_for_user(session_id, user_id)
        buckets = {'correct': [], 'wrong': [], 'skipped': []}
        for item in review_items:
            qrow = base.DBH.fetchone('SELECT message_id FROM session_questions WHERE session_id=? AND q_no=?', (session_id, item['q_no']))
            link = base.get_message_link(int(session['chat_id']), int(qrow['message_id'] or 0), username) if qrow else None
            label = f'<a href="{link}">Q{item["q_no"]}</a>' if link else f'Q{item["q_no"]}'
            buckets[item['status']].append(label)
        correct = int(rank_item['correct'])
        wrong = int(rank_item['wrong'])
        skipped = int(rank_item['skipped'])
        attempted = max(1, correct + wrong)
        accuracy = (correct / attempted) * 100.0
        percentage = (correct / max(1, int(session['total_questions']))) * 100.0
        percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
        duration_seconds = max(0, int((session['ended_at'] or base.now_ts()) - (session['started_at'] or base.now_ts())))
        lines = [
            f'<b>{base.html_escape(session["title"])}</b>',
            '',
            #f'Rank: <b>#{rank_item["rank"]}</b> / {total_users}',
            f'৻ꪆ Score: <b>{rank_item["score"]}</b>\n',
            f'✅ Correct: <b>{correct}</b>   ❌ Wrong: <b>{wrong}</b>   ➖ Skipped: <b>{skipped}</b>\n',
            f'Negative / wrong: <b>{session["negative_mark"]}</b>',
            f'🎯 Accuracy: <b>{accuracy:.2f}%</b>\n📊 Percentage: <b>{percentage:.2f}%</b>',
            f'⏱ Time: <b>{duration_seconds}s</b>',
            ''
        ]
        if section_data and (len(section_data) > 1 or section_data[0]['title'] != 'General'):
            lines.append('<b>Section Analysis</b>')
            for item in section_data:
                lines.append(f'• {base.html_escape(item["title"])} — ✅ {item["correct"]}  ❌ {item["wrong"]}  ➖ {item["skipped"]}')
            lines.append('')
        lines.extend(['<b>Correct</b>', ', '.join(buckets['correct']) or '—', '', '<b>Wrong</b>', ', '.join(buckets['wrong']) or '—', '', '<b>Skipped</b>', ', '.join(buckets['skipped']) or '—'])
        buttons: List[List[InlineKeyboardButton]] = []
        practice_url = _build_practice_url_v4(context.bot_data.get('bot_username', ''), str(session['draft_id']), int(session['created_by']))
        if practice_url:
            buttons.append([InlineKeyboardButton('🔁 Try Again', url=practice_url)])
        with suppress(TelegramError):
            await context.bot.send_message(user_id, '\n'.join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, disable_web_page_preview=True)
        with suppress(Exception):
            html_doc = render_user_result_html(session, p, rank_item, ranking, review_items, section_data)
            await context.bot.send_document(user_id, document=InputFile(base.io.BytesIO(html_doc.encode('utf-8')), filename=f"{base.pdf_safe_filename(session['title'])}_result.html"), caption='Professional HTML result report.')


base.send_private_results = send_private_results


def _draft_prefix_state(draft: Any) -> bool:
    try:
        raw = draft['show_title_prefix']
    except Exception:
        raw = None
    if raw is None:
        return True
    return str(raw).strip().lower() not in {'0', 'false', 'off', 'no'}


async def begin_or_advance_exam(context, session_id: str) -> None:
    session = base.get_session(session_id)
    if not session or session['status'] != 'running':
        return
    next_index = int(session['current_index'] or 0) + 1
    total = int(session['total_questions'] or 0)
    if next_index > total:
        await base.finish_exam(context, session_id, reason='completed')
        return
    q = base.get_session_question(session_id, next_index)
    if not q:
        await base.finish_exam(context, session_id, reason='missing_question')
        return
    raw_options = [str(x) for x in (base.jload(q['options'], []) or [])]
    clean_options = [_latex_to_pretty_text(_smart_clean_option_text(x)) or chr(65+i) for i, x in enumerate(raw_options)]
    section_title = base.normalize_visual_text(q['section_title'] or '')
    base_seconds = int(q['question_time_override'] or session['question_time'] or 30)
    speed_factor = float(session['speed_factor'] or 1.0)
    effective_seconds = max(5, int(round(base_seconds * speed_factor)))
    draft_row = base.get_draft(str(session['draft_id'])) if session['draft_id'] else None
    show_title = _draft_prefix_state(draft_row)
    q_text = _smart_clean_question_text(str(q['question'] or '')) or f'Question {next_index}'
    prefix_parts = [f'[{next_index}/{total}]']
    if section_title:
        prefix_parts.append(f'[{section_title}]')
    if show_title:
        prefix_parts.append(f'[{base.normalize_visual_text(session["title"])}]')
    question_prefix = (' '.join(prefix_parts) + '\n') if prefix_parts else ''
    poll_question = (question_prefix + _latex_to_pretty_text(q_text)).strip() or f'Question {next_index}'
    if len(poll_question) > 300:
        allowed_q = max(10, 300 - len(question_prefix))
        poll_question = question_prefix + _latex_to_pretty_text(q_text)[: allowed_q - 1].rstrip() + '…'
    explanation_text = _latex_to_pretty_text(_smart_clean_explanation_text(str(q['explanation'] or f'Question {next_index} of {total}')))
    if len(explanation_text) > 200:
        explanation_text = explanation_text[:199] + '…'
    image_bytes = _render_poll_question_image(q_text, raw_options)
    poll_options = clean_options
    poll_label = poll_question
    if image_bytes:
        poll_label = 'What is the answer?'
        poll_options = [chr(65 + i) for i in range(len(raw_options))]
    try:
        if image_bytes:
            await context.bot.send_photo(chat_id=session['chat_id'], photo=InputFile(_io.BytesIO(image_bytes), filename=f'q_{next_index}.png'), caption=' '.join(prefix_parts) or f'Question {next_index}/{total}')
        msg = await context.bot.send_poll(chat_id=session['chat_id'], question=poll_label, options=poll_options, type=Poll.QUIZ, is_anonymous=False, allows_multiple_answers=False, correct_option_id=int(q['correct_option']), explanation=explanation_text or f'Question {next_index} of {total}', open_period=effective_seconds)
    except TelegramError as exc:
        base.logger.exception('Failed to send poll: %s', exc)
        await base.finish_exam(context, session_id, reason='send_poll_error')
        return
    poll_id = msg.poll.id
    with closing(base.DBH.connect()) as conn:
        conn.execute('UPDATE session_questions SET poll_id=?, message_id=?, open_ts=?, close_ts=? WHERE session_id=? AND q_no=?', (poll_id, msg.message_id, base.now_ts(), base.now_ts() + effective_seconds, session_id, next_index))
        conn.execute('UPDATE sessions SET current_index=?, active_poll_id=?, active_poll_message_id=? WHERE id=?', (next_index, poll_id, msg.message_id, session_id))
        conn.commit()
    context.job_queue.run_once(base.close_poll_job, when=max(1, effective_seconds), data={'session_id': session_id, 'q_no': next_index}, name=f'close:{session_id}:{next_index}')


base.begin_or_advance_exam = begin_or_advance_exam


async def handle_inline_query(update: Update, context) -> None:
    iq = update.inline_query
    if not iq or not iq.from_user:
        return
    user_id = iq.from_user.id
    if not base.user_has_staff_access(user_id):
        await iq.answer([], cache_time=0, is_personal=True)
        return
    query = base.normalize_visual_text(iq.query or '')
    drafts = base.list_user_drafts(user_id)
    bot_username = context.bot_data.get('bot_username', '')
    results: List[InlineQueryResultArticle] = []
    for row in drafts[:20]:
        q_count = int(row.get('q_count', 0) if isinstance(row, dict) else row['q_count'])
        if q_count <= 0:
            continue
        title = str(row['title'])
        code = str(row['id'])
        q_lower = query.casefold()
        if query and not (q_lower in code.casefold() or q_lower in title.casefold() or q_lower in f'quiz:{code.casefold()}'):
            continue
        practice_url = _build_practice_url_v4(bot_username, code, int(row['owner_id'])) if bot_username else None
        prefix = 'ON' if _draft_prefix_state(row) else 'OFF'
        text = (
            f"<b>{base.html_escape(title)}</b>\n"
            f"Quiz ID: <code>{code}</code>\n"
            f"Questions: <b>{q_count}</b>\n"
            f"Time / question: <b>{row['question_time']} sec</b>\n"
            f"Negative / wrong: <b>{row['negative_mark']}</b>\n"
            f"Title prefix: <b>{prefix}</b>"
        )
        if practice_url:
            text += f"\n\nPractice link:\n{practice_url}"
        results.append(
            InlineQueryResultArticle(
                id=code,
                title=f"{title}",
                description=f"ID {code} • {q_count} questions • {row['question_time']} sec • -{row['negative_mark']}",
                input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML),
            )
        )
    await iq.answer(results, cache_time=0, is_personal=True)



if __name__ == "__main__":
    base.main()

# ============================================================
# Final patch v9: robust text import, fixed HTML export,
# improved unicode math images, better responsive HTML exam
# ============================================================
from PIL import ImageFont as _ImageFont

_OLD_PARSE_MARKED_QUESTIONS = parse_marked_questions_from_text

_UNICODE_FONT_CANDIDATES = [
    '/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansBengaliUI-Regular.ttf',
    '/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf',
    '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]
_UNICODE_BOLD_FONT_CANDIDATES = [
    '/usr/share/fonts/truetype/noto/NotoSansBengali-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansBengaliUI-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf',
]


def _load_unicode_font(size: int, bold: bool = False):
    candidates = _UNICODE_BOLD_FONT_CANDIDATES if bold else _UNICODE_FONT_CANDIDATES
    for path in candidates:
        try:
            return _ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return _ImageFont.load_default()


def _latex_to_pretty_text(raw: str) -> str:
    text = str(raw or '')
    if not text:
        return ''
    text = text.replace('\r', '')
    text = text.replace('\\(', '').replace('\\)', '')
    text = text.replace('\\[', '').replace('\\]', '')
    text = text.replace('$$', '').replace('$', '')
    text = text.replace('\\left', '').replace('\\right', '')
    text = text.replace('\\,', ' ').replace('\\;', ' ').replace('\\:', ' ')
    text = text.replace('\\!', '')

    frac_re = re.compile(r'\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}')
    sqrt_re = re.compile(r'\\sqrt\s*\{([^{}]+)\}')
    text_re = re.compile(r'\\(?:mathrm|text|operatorname)\{([^{}]+)\}')
    while frac_re.search(text):
        text = frac_re.sub(lambda m: f"({m.group(1)})/({m.group(2)})", text)
    while sqrt_re.search(text):
        text = sqrt_re.sub(lambda m: f"√({m.group(1)})", text)
    while text_re.search(text):
        text = text_re.sub(lambda m: m.group(1), text)

    replacements = {
        '\\cdot': '·', '\\times': '×', '\\div': '÷', '\\pm': '±', '\\mp': '∓',
        '\\neq': '≠', '\\ne': '≠', '\\leq': '≤', '\\geq': '≥', '\\le': '≤', '\\ge': '≥',
        '\\infty': '∞', '\\pi': 'π', '\\theta': 'θ', '\\alpha': 'α', '\\beta': 'β', '\\gamma': 'γ',
        '\\delta': 'δ', '\\Delta': 'Δ', '\\lambda': 'λ', '\\mu': 'μ', '\\sigma': 'σ', '\\omega': 'ω',
        '\\Omega': 'Ω', '\\phi': 'φ', '\\Phi': 'Φ', '\\sum': '∑', '\\prod': '∏', '\\int': '∫',
        '\\lim': 'lim', '\\sin': 'sin', '\\cos': 'cos', '\\tan': 'tan', '\\cot': 'cot', '\\sec': 'sec',
        '\\csc': 'csc', '\\ln': 'ln', '\\log': 'log', '\\cup': '∪', '\\cap': '∩', '\\subseteq': '⊆',
        '\\subset': '⊂', '\\supseteq': '⊇', '\\supset': '⊃', '\\Rightarrow': '⇒', '\\rightarrow': '→',
        '\\to': '→', '\\leftarrow': '←', '\\approx': '≈', '\\therefore': '∴', '\\because': '∵',
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)

    text = re.sub(r'\^\{([^{}]+)\}', lambda m: '^(' + m.group(1) + ')', text)
    text = re.sub(r'\^([A-Za-z0-9+\-])', lambda m: '^' + m.group(1), text)
    text = re.sub(r'_\{([^{}]+)\}', lambda m: '_(' + m.group(1) + ')', text)
    text = re.sub(r'_([A-Za-z0-9+\-])', lambda m: '_' + m.group(1), text)
    text = text.replace('{', '').replace('}', '')
    text = re.sub(r'\\([A-Za-z]+)', r'\1', text)
    text = text.replace('\\', '')
    text = re.sub(r'\s*⇒\s*', ' ⇒ ', text)
    text = re.sub(r'\s*→\s*', ' → ', text)
    text = re.sub(r'\s+', ' ', text)
    return base.normalize_visual_text(text).strip()


def _html_from_display_text(raw: str) -> str:
    return base.html_escape(_latex_to_pretty_text(raw)).replace('\n', '<br>')


def _mathjax_html(raw: str) -> str:
    value = str(raw or '')
    if not value:
        return ''
    return base.html_escape(base.normalize_visual_text(value)).replace('\n', '<br>')


def _contains_math_markup(text: str) -> bool:
    return bool(text and re.search(r'(\\frac|\\sqrt|\\left|\\right|\\pi|\\theta|\\alpha|\\beta|\\gamma|\\lim|\\int|\\sum|\\displaystyle|\\textstyle|\$|\\\(|\\\)|\\\[|\\\]|\^|_)', text))


def _render_readable_text_image(text: str, width: int = 1280, font_size: int = 34, padding: int = 42, bg: str = '#ffffff', fg: str = '#101827', title: str = '') -> Optional[bytes]:
    text = _latex_to_pretty_text(text)
    if not text:
        return None
    try:
        probe = _Image.new('RGB', (width, 200), bg)
        probe_draw = _ImageDraw.Draw(probe)
        title_font = _load_unicode_font(max(28, font_size + 6), bold=True)
        body_font = _load_unicode_font(font_size, bold=False)
        y = padding
        total_h = padding
        if title:
            _, title_bottom = base.draw_multiline(probe_draw, title, (padding, y), title_font, fg, width - 2 * padding, line_gap=6)
            total_h = max(total_h, title_bottom)
            y = title_bottom + 12
        lines = base.wrap_text(probe_draw, text, body_font, width - 2 * padding)
        line_h = max(34, font_size + 14)
        total_h = y + (len(lines) * line_h) + padding
        img = _Image.new('RGB', (width, total_h), bg)
        draw = _ImageDraw.Draw(img)
        y = padding
        if title:
            _, title_bottom = base.draw_multiline(draw, title, (padding, y), title_font, fg, width - 2 * padding, line_gap=6)
            y = title_bottom + 12
        for line in lines:
            draw.text((padding, y), line, font=body_font, fill=fg)
            y += line_h
        out = _io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _render_poll_question_image(question: str, options: List[str]) -> Optional[bytes]:
    needs_image = _contains_math_markup(question) or any(_contains_math_markup(x) for x in options)
    if not needs_image:
        return None
    pretty_q = _latex_to_pretty_text(question)
    pretty_opts = [_latex_to_pretty_text(x) for x in options]
    try:
        width = 1600
        padding = 58
        bg = '#ffffff'
        fg = '#101827'
        accent = '#2563eb'
        border = '#d5dbe7'
        probe = _Image.new('RGB', (width, 200), bg)
        pd = _ImageDraw.Draw(probe)
        watermark_font = _load_unicode_font(18, bold=True)
        q_font = _load_unicode_font(42, bold=False)
        opt_font = _load_unicode_font(34, bold=False)
        q_lines = base.wrap_text(pd, pretty_q, q_font, width - 2 * padding)
        total_h = padding + (len(q_lines) * 54) + 40
        for idx, opt in enumerate(pretty_opts):
            opt_lines = base.wrap_text(pd, f'{chr(65+idx)}. {opt}', opt_font, width - 2 * padding - 36)
            total_h += max(64, len(opt_lines) * 46 + 18) + 16
        total_h += 64
        img = _Image.new('RGB', (width, total_h), bg)
        draw = _ImageDraw.Draw(img)
        draw.rounded_rectangle((16, 16, width - 16, total_h - 16), radius=28, outline=border, width=2, fill=bg)
        y = padding
        for line in q_lines:
            draw.text((padding, y), line, font=q_font, fill=fg)
            y += 54
        y += 10
        for idx, opt in enumerate(pretty_opts):
            label = f'{chr(65+idx)}. {opt}'
            opt_lines = base.wrap_text(draw, label, opt_font, width - 2 * padding - 36)
            box_h = max(64, len(opt_lines) * 46 + 18)
            draw.rounded_rectangle((padding - 10, y - 8, width - padding + 10, y + box_h - 6), radius=22, outline=border, width=2, fill='#fbfbfd')
            yy = y + 6
            for line in opt_lines:
                draw.text((padding + 12, yy), line, font=opt_font, fill=fg)
                yy += 46
            y += box_h + 16
        watermark = 'Target Quiz Bot'
        wb = draw.textbbox((0, 0), watermark, font=watermark_font)
        draw.text((width - padding - (wb[2]-wb[0]), total_h - padding + 8), watermark, font=watermark_font, fill=accent)
        out = _io.BytesIO()
        img.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _parse_numbered_question_format(text: str) -> List[Dict[str, Any]]:
    raw = (text or '').replace('\r', '').strip()
    if not raw:
        return []
    raw = re.sub(r'\n\s*[nN]\s*\n', '\n\n', raw)
    raw = re.sub(r'\n\s*(?=\d+\.\s)', '\n\u0000', raw)
    parts = [p.strip() for p in raw.split('\u0000') if p.strip()]
    parsed: List[Dict[str, Any]] = []
    opt_re = re.compile(r'^\s*\(?([A-Ja-j])\)?[\).]\s*(.+?)\s*$')
    expl_re = re.compile(r'^\s*(?:ব্যাখ্যা|explanation|explain|reason|note)\s*[:：-]\s*(.*)\s*$', re.I)
    ans_re = re.compile(r'^\s*(?:answer|ans|correct|right|উত্তর)\s*[:：-]\s*(.+?)\s*$', re.I)
    for part in parts:
        lines = [base.normalize_visual_text(x).strip() for x in part.split('\n') if base.normalize_visual_text(x).strip() and base.normalize_visual_text(x).strip().lower() != 'n']
        if not lines:
            continue
        question_parts: List[str] = []
        options: List[str] = []
        explanation_parts: List[str] = []
        answer_ref: Optional[str] = None
        correct_idx: Optional[int] = None
        mode = 'question'
        for line_no, line in enumerate(lines):
            line = re.sub(r'^\s*\d+\.\s*', '', line) if line_no == 0 else line
            em = expl_re.match(line)
            if em:
                mode = 'explanation'
                if em.group(1).strip():
                    explanation_parts.append(em.group(1).strip())
                continue
            am = ans_re.match(line)
            if am:
                answer_ref = am.group(1).strip()
                continue
            om = opt_re.match(line)
            if om:
                mode = 'option'
                text_part = om.group(2).strip()
                marked = text_part.endswith('*') or any(mark in text_part for mark in CHECKMARKS)
                for mark in CHECKMARKS:
                    text_part = text_part.replace(mark, '')
                text_part = text_part.rstrip('*').strip()
                if text_part:
                    options.append(_smart_clean_option_text(text_part))
                    if marked:
                        correct_idx = len(options) - 1
                continue
            if mode == 'question' and not options:
                question_parts.append(line)
            elif mode == 'option' and options and not explanation_parts:
                options[-1] = base.normalize_visual_text(options[-1] + ' ' + line).strip()
            else:
                explanation_parts.append(line)
        question = _smart_clean_question_text(' '.join(question_parts))
        if correct_idx is None and answer_ref:
            correct_idx = parse_answer_ref(answer_ref, options)
        if question and len(options) >= 2 and correct_idx is not None:
            parsed.append({
                'question': question,
                'options': options,
                'correct_option': int(correct_idx),
                'explanation': _smart_clean_explanation_text(' '.join(explanation_parts)),
            })
    return parsed


def parse_marked_questions_from_text(text: str) -> List[Dict[str, Any]]:
    raw = (text or '').replace('\r', '').strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
        if isinstance(payload, list):
            items: List[Dict[str, Any]] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                q = _smart_clean_question_text(str(item.get('question') or item.get('questions') or ''))
                opts = item.get('options') or []
                if isinstance(opts, dict):
                    opts = list(opts.values())
                opts = [_smart_clean_option_text(str(x)) for x in opts if str(x).strip()]
                ans = parse_answer_ref(str(item.get('answer') or item.get('correct') or ''), opts)
                if q and len(opts) >= 2 and ans is not None:
                    items.append({'question': q, 'options': opts, 'correct_option': ans, 'explanation': _smart_clean_explanation_text(str(item.get('explanation') or ''))})
            if items:
                return items
    except Exception:
        pass
    numbered = _parse_numbered_question_format(raw)
    if numbered:
        return numbered
    return _OLD_PARSE_MARKED_QUESTIONS(raw)


def render_scroll_exam_html(draft: Any, owner_id: int) -> str:
    theme = _export_theme_palette(owner_id, draft)
    questions = _draft_question_rows_with_sections(str(draft['id']))
    if not questions:
        raise ValueError('Draft has no valid questions.')
    title_text = base.normalize_visual_text(str(draft['title'] or 'Exam'))
    title_html = base.html_escape(title_text)
    sections = sorted({str(q['section']) for q in questions})
    export_rows = []
    for q in questions:
        q_raw = str(q['question'] or '')
        q_has_math = _contains_math_markup(q_raw)
        export_rows.append({
            'q_no': int(q['q_no']),
            'section': str(q['section']),
            'question_raw': _mathjax_html(q_raw),
            'question_pretty': _html_from_display_text(q_raw),
            'question_has_math': q_has_math,
            'question_image': _png_data_uri(_render_readable_text_image(q_raw, width=1400, font_size=36) if q_has_math else None),
            'options': [{
                'raw': _mathjax_html(str(opt)),
                'pretty': _html_from_display_text(_display_option_text(str(opt), idx)),
                'has_math': _contains_math_markup(str(opt)),
            } for idx, opt in enumerate(q['options'])],
            'correct': int(q['correct_option']),
            'explanation_raw': _mathjax_html(str(q['explanation'] or '')),
            'explanation_pretty': _html_from_display_text(str(q['explanation'] or '')),
            'explanation_has_math': _contains_math_markup(str(q['explanation'] or '')),
        })
    tpl = """<!DOCTYPE html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'><title>__TITLE__</title>
<style>
:root{--accent:__ACCENT__;--accent-soft:__ACCENT_SOFT__;--danger:__DANGER__;--success:__SUCCESS__;--warning:__WARNING__;--radius:22px;--shadow:0 14px 38px rgba(15,23,42,.10)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,Arial,'Noto Sans Bengali',sans-serif;background:var(--page-bg);color:var(--text);transition:.2s;background-attachment:fixed}
body[data-theme='light']{--page-bg:linear-gradient(180deg,#f8fbff,#eff4fb 55%,#f7fafc);--text:#0f172a;--muted:#5b6778;--surface:#ffffff;--surface-2:#f8fafc;--border:#dbe4f0;--glass:rgba(255,255,255,.84);--chip:rgba(37,99,235,.08)}
body[data-theme='dark']{--page-bg:radial-gradient(circle at top,#10213c 0,#05070b 52%,#020407 100%);--text:#f7fafc;--muted:#94a3b8;--surface:#0a1323;--surface-2:#0e1a2f;--border:rgba(148,163,184,.18);--glass:rgba(5,7,11,.9);--chip:rgba(148,163,184,.12)}
img{max-width:100%;display:block}.page{display:none;min-height:100vh}.page.active{display:block}.shell{width:min(1120px,100% - 24px);margin-inline:auto}
.topbar{position:fixed;inset:0 0 auto 0;z-index:90;background:var(--glass);backdrop-filter:blur(18px);border-bottom:1px solid var(--border)}.topbar .inner{width:min(1120px,100% - 24px);margin:auto;display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 0}.brand h1{margin:0;font-size:clamp(22px,3.8vw,34px);line-height:1.1}.meta{font-size:clamp(12px,2vw,15px);color:var(--muted)}
.timer{padding:12px 16px;border-radius:18px;background:var(--surface);border:1px solid var(--border);min-width:120px;text-align:center;box-shadow:var(--shadow)}.timer .label{font-size:12px;color:var(--muted)}.timer .value{font-size:clamp(22px,4vw,30px);font-weight:900}
.hero{padding:34px 0 20px}.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow)}.start-card{padding:28px;display:grid;gap:18px}.headline{font-size:clamp(30px,5vw,46px);font-weight:900;line-height:1.08}.muted{color:var(--muted)}.submeta{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:clamp(14px,2vw,18px)}
.input{width:100%;padding:16px 18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-size:16px;outline:none}.input:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(37,99,235,.14)}
.chips{display:flex;flex-wrap:wrap;gap:12px}.chip{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:16px;background:var(--chip);border:1px solid var(--border);font-weight:700}.chip input{accent-color:var(--accent)}.actions{display:flex;flex-wrap:wrap;gap:12px}.btn{border:0;border-radius:18px;padding:15px 20px;font-weight:800;font-size:16px;cursor:pointer;transition:.18s ease transform,.18s ease filter}.btn:hover{transform:translateY(-1px)}.btn.primary{background:var(--accent);color:#fff}.btn.secondary{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.exam-wrap{padding-top:98px;padding-bottom:96px}.exam-grid{display:grid;gap:18px}.question-card{padding:20px;scroll-margin-top:90px;background:linear-gradient(180deg,var(--surface),var(--surface-2))}.q-top{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px}.q-index{font-size:clamp(24px,4vw,36px);font-weight:900;color:#c8f7bf}.q-section{padding:8px 14px;border-radius:999px;background:var(--chip);border:1px solid var(--border);font-weight:700}.q-text{font-size:clamp(18px,3vw,30px);line-height:1.6;font-weight:800;word-break:break-word}.q-text.small{font-size:clamp(17px,2.8vw,26px)}.q-image{border-radius:18px;border:1px solid var(--border);margin:14px 0;background:#fff;overflow:hidden}.options{display:grid;gap:14px;margin-top:18px}.opt{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:flex-start;padding:16px 18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2);cursor:pointer;transition:.18s ease;border-color:.18s ease}.opt.selected{border-color:var(--accent);background:var(--accent-soft)}.opt.locked{cursor:not-allowed;opacity:.95}.opt input{margin-top:4px;width:20px;height:20px;accent-color:var(--accent)}.opt-body{font-size:clamp(17px,2.7vw,25px);line-height:1.55;word-break:break-word}.opt-label{font-weight:900;margin-right:10px}
.float-actions{position:fixed;right:14px;bottom:14px;display:flex;gap:12px;z-index:95}.fab{height:56px;padding:0 18px;border-radius:999px;border:1px solid var(--border);background:var(--glass);backdrop-filter:blur(12px);color:var(--text);font-weight:900;box-shadow:var(--shadow);cursor:pointer}.fab.primary{background:var(--accent);color:#fff;border-color:transparent}
.drawer-backdrop{position:fixed;inset:0;background:rgba(2,6,23,.32);opacity:0;pointer-events:none;transition:.2s ease;z-index:96}.drawer-backdrop.show{opacity:1;pointer-events:auto}.drawer{position:fixed;left:0;right:0;bottom:0;transform:translateY(105%);transition:.22s ease;z-index:97;background:var(--surface);border-top-left-radius:28px;border-top-right-radius:28px;border:1px solid var(--border);padding:22px;max-height:78vh;overflow:auto;box-shadow:0 -18px 48px rgba(2,6,23,.16)}.drawer.show{transform:translateY(0)}.drawer h3{margin:0 0 14px 0}.palette{display:grid;grid-template-columns:repeat(auto-fill,minmax(52px,1fr));gap:12px}.bubble{height:52px;border-radius:16px;border:1px solid var(--border);display:grid;place-items:center;font-weight:900;background:var(--surface-2);cursor:pointer}.bubble.answered{background:rgba(22,163,74,.16);border-color:rgba(22,163,74,.46)}.bubble.current{outline:3px solid var(--accent)}
.result-wrap{padding:28px 0 34px}.hero-result{padding:26px}.score-ring{font-size:clamp(54px,10vw,98px);font-weight:900;line-height:1;color:var(--accent)}.result-title{font-size:clamp(28px,4vw,40px);font-weight:900}.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-top:18px}.stat{padding:18px;border-radius:20px;background:var(--surface-2);border:1px solid var(--border)}.stat .label{font-size:13px;color:var(--muted);font-weight:700}.stat .value{font-size:clamp(26px,4vw,40px);font-weight:900;margin-top:8px}.section-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.bar{height:10px;border-radius:999px;background:rgba(148,163,184,.18);overflow:hidden;margin-top:10px}.bar>span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),#ef4444)}.tabs{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.tab{padding:12px 16px;border-radius:16px;border:1px solid var(--border);background:var(--surface-2);font-weight:800;cursor:pointer}.tab.active{background:var(--accent);color:#fff;border-color:transparent}.review-list{display:grid;gap:16px}.review-card{padding:18px;border-radius:18px;border:1px solid var(--border);background:var(--surface-2)}.review-card.correct{border-left:5px solid var(--success)}.review-card.wrong{border-left:5px solid var(--danger)}.review-card.skipped{border-left:5px solid var(--warning)}.review-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}.review-q{font-size:clamp(17px,2.7vw,22px);font-weight:800;line-height:1.55}.answer-line{font-size:15px;line-height:1.65;margin-top:8px}
@media(max-width:720px){.topbar .inner{padding:12px 0}.timer{min-width:106px;padding:10px 12px}.question-card{padding:18px}.float-actions{right:12px;bottom:12px}.fab{height:54px;padding:0 16px}.shell{width:min(1120px,100% - 16px)}}
</style>
<script>window.MathJax={tex:{inlineMath:[["\\(","\\)"],["$","$"]],displayMath:[["\\[","\\]"],["$$","$$"]]},svg:{fontCache:'global'}};</script>
<script defer src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js'></script>
</head><body data-theme='__DEFAULT_MODE__'>
<div id='startPage' class='page active'><div class='hero shell'><div class='start-card card'><div class='headline'>__TITLE__</div><div class='submeta muted'>Questions: __QCOUNT__ • Time / question: __QTIME__ sec • Negative: __NEG__</div><input id='studentName' class='input' type='text' placeholder='Enter your name'><div><div style='font-size:22px;font-weight:900;margin-bottom:10px'>Select sections</div><div id='sectionBox' class='chips'></div></div><div class='actions'><button id='startBtn' class='btn primary'>Start HTML Exam</button><button id='toggleThemeBtn' class='btn secondary'>Toggle Theme</button></div></div></div></div>
<div id='examPage' class='page'><div class='topbar'><div class='inner'><div class='brand'><h1>__TITLE__</h1><div id='metaLine' class='meta'>Loading exam…</div></div><div class='timer'><div class='label'>Remaining</div><div id='timerValue' class='value'>00:00</div></div></div></div><div class='exam-wrap shell'><div id='questionList' class='exam-grid'></div></div><div class='float-actions'><button id='jumpBtn' class='fab'>Sections</button><button id='submitBtn' class='fab primary'>Submit</button></div><div id='drawerBackdrop' class='drawer-backdrop'></div><div id='drawer' class='drawer'><h3>Sections & Question List</h3><div id='sectionJump' class='chips' style='margin-bottom:16px'></div><div id='palette' class='palette'></div></div></div>
<div id='resultPage' class='page'><div class='result-wrap shell'><div class='hero-result card'><div class='muted'>__TITLE__</div><div id='resultName' class='result-title'>Result</div><div id='resultScore' class='score-ring'>0.00</div><div class='muted'>Professional performance report</div><div id='summaryGrid' class='summary-grid'></div></div><div class='card' style='padding:22px;margin-top:16px'><div style='font-size:24px;font-weight:900;margin-bottom:12px'>Section Analysis</div><div id='sectionResultGrid' class='section-grid'></div></div><div class='card' style='padding:22px;margin-top:16px'><div style='font-size:24px;font-weight:900'>Answer Review</div><div class='tabs'><button class='tab active' data-filter='all'>All</button><button class='tab' data-filter='correct'>Correct</button><button class='tab' data-filter='wrong'>Wrong</button><button class='tab' data-filter='skipped'>Skipped</button></div><div id='reviewList' class='review-list'></div></div></div></div>
<script>
const QUESTIONS=__DATA__; const SECTIONS=__SECTIONS__; const NEGATIVE_MARK=__NEG_FLOAT__; const QUESTION_TIME=__QTIME__; let selectedSections=new Set(SECTIONS), active=[], answers={}, totalSeconds=0, leftSeconds=0, timer=null, currentQuestion=0;
const $=(id)=>document.getElementById(id); function fmt(sec){sec=Math.max(0,Math.floor(sec));const m=Math.floor(sec/60),s=sec%60;return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');} function typesetMath(){if(window.MathJax&&window.MathJax.typesetPromise){window.MathJax.typesetPromise().catch(()=>{});}} function toggleTheme(){document.body.setAttribute('data-theme',document.body.getAttribute('data-theme')==='dark'?'light':'dark');}
$('toggleThemeBtn').onclick=toggleTheme;
function buildSectionSelectors(){const box=$('sectionBox'); box.innerHTML=''; SECTIONS.forEach(sec=>{const label=document.createElement('label'); label.className='chip'; label.innerHTML=`<input type='checkbox' checked> <span>${sec}</span>`; const input=label.querySelector('input'); input.onchange=()=>{ if(input.checked) selectedSections.add(sec); else selectedSections.delete(sec);}; box.appendChild(label);});}
function showPage(id){['startPage','examPage','resultPage'].forEach(x=>$(x).classList.remove('active')); $(id).classList.add('active');}
function renderOption(option,idx){ return option.has_math ? `<div class='opt-body'><span class='opt-label'>${String.fromCharCode(65+idx)}.</span> <span class='math'>${option.raw}</span></div>` : `<div class='opt-body'><span class='opt-label'>${String.fromCharCode(65+idx)}.</span>${option.pretty}</div>`; }
function renderQuestionBlock(q){ if(q.question_has_math){ if(q.question_image){ return `<div class='q-image'><img src='${q.question_image}' alt='Question ${q.q_no}'></div>`; } return `<div class='q-text math'>${q.question_raw}</div>`; } return `<div class='q-text ${q.question_pretty.length>170?'small':''}'>${q.question_pretty}</div>`; }
function lockQuestion(idx,opt){ if(answers[idx]!==undefined) return; answers[idx]=opt; document.querySelectorAll(`.opt[data-idx="${idx}"]`).forEach(el=>{el.classList.add('locked'); const radio=el.querySelector('input'); if(radio) radio.disabled=true;}); const selected=document.querySelector(`.opt[data-idx="${idx}"][data-opt="${opt}"]`); if(selected) selected.classList.add('selected'); updatePalette(); currentQuestion=idx; const next=document.getElementById(`q-${idx+1}`); if(next){ setTimeout(()=>next.scrollIntoView({behavior:'smooth', block:'start'}),180); } }
function renderExam(){ active=QUESTIONS.filter(q=>selectedSections.has(q.section)); if(!active.length) active=[...QUESTIONS]; answers={}; currentQuestion=0; totalSeconds=active.length*QUESTION_TIME; leftSeconds=totalSeconds; $('metaLine').textContent=`${active.length} questions • sections: ${[...new Set(active.map(q=>q.section))].join(', ')}`; const list=$('questionList'); list.innerHTML=''; active.forEach((q,idx)=>{ const card=document.createElement('div'); card.className='question-card card'; card.id=`q-${idx}`; const options=q.options.map((opt,i)=>`<label class='opt' data-idx='${idx}' data-opt='${i}'><input type='radio' name='q_${idx}'><div>${renderOption(opt,i)}</div></label>`).join(''); card.innerHTML=`<div class='q-top'><div><span class='q-index'>[${idx+1}/${active.length}]</span> <span class='q-section'>${q.section}</span></div></div>${renderQuestionBlock(q)}<div class='options'>${options}</div>`; list.appendChild(card); }); document.querySelectorAll('.opt').forEach(el=>{ el.addEventListener('click',()=>{ const idx=Number(el.dataset.idx),opt=Number(el.dataset.opt); lockQuestion(idx,opt); }); }); buildDrawer(); updatePalette(); typesetMath(); }
function buildDrawer(){ const jump=$('sectionJump'); jump.innerHTML=''; [...new Set(active.map(q=>q.section))].forEach(sec=>{ const btn=document.createElement('button'); btn.className='btn secondary'; btn.textContent=sec; btn.onclick=()=>{ const row=active.findIndex(x=>x.section===sec); if(row>=0){ currentQuestion=row; document.getElementById(`q-${row}`).scrollIntoView({behavior:'smooth', block:'start'}); closeDrawer(); updatePalette(); } }; jump.appendChild(btn); }); const palette=$('palette'); palette.innerHTML=''; active.forEach((q,idx)=>{ const bubble=document.createElement('button'); bubble.className='bubble'; bubble.textContent=idx+1; bubble.onclick=()=>{ currentQuestion=idx; document.getElementById(`q-${idx}`).scrollIntoView({behavior:'smooth', block:'start'}); closeDrawer(); updatePalette(); }; if(answers[idx]!==undefined) bubble.classList.add('answered'); if(idx===currentQuestion) bubble.classList.add('current'); palette.appendChild(bubble); }); }
function updatePalette(){ buildDrawer(); } function openDrawer(){ $('drawerBackdrop').classList.add('show'); $('drawer').classList.add('show'); } function closeDrawer(){ $('drawerBackdrop').classList.remove('show'); $('drawer').classList.remove('show'); }
$('jumpBtn').onclick=openDrawer; $('drawerBackdrop').onclick=closeDrawer; function startTimer(){ clearInterval(timer); $('timerValue').textContent=fmt(leftSeconds); timer=setInterval(()=>{ leftSeconds-=1; $('timerValue').textContent=fmt(leftSeconds); if(leftSeconds<=0){ clearInterval(timer); finishExam(); } },1000); }
function buildSummaryCards(summary){ $('summaryGrid').innerHTML=summary.map(item=>`<div class='stat'><div class='label'>${item.label}</div><div class='value'>${item.value}</div></div>`).join(''); }
function reviewBlock(item){ const qBlock=item.question_has_math?(item.question_image?`<div class='q-image' style='margin-bottom:10px'><img src='${item.question_image}' alt='Question'></div>`:`<div class='review-q math'>${item.question_raw}</div>`):`<div class='review-q'>${item.question_pretty}</div>`; const ansRaw=item.answer_has_math?`<span class='math'>${item.answer_raw}</span>`:item.answer_pretty; const corRaw=item.correct_has_math?`<span class='math'>${item.correct_raw}</span>`:item.correct_pretty; const exp=item.explanation_raw?(item.explanation_has_math?`<div class='answer-line muted'><b>Explanation:</b> <span class='math'>${item.explanation_raw}</span></div>`:`<div class='answer-line muted'><b>Explanation:</b> ${item.explanation_pretty}</div>`):''; return `<div class='review-card ${item.status}' data-status='${item.status}'><div class='review-head'><div><b>Q${item.q_no}</b> • ${item.section}</div><div class='muted'>${item.status.toUpperCase()}</div></div>${qBlock}<div class='answer-line'><b>Your answer:</b> ${ansRaw}</div><div class='answer-line'><b>Correct answer:</b> ${corRaw}</div>${exp}</div>`; }
function finishExam(){ clearInterval(timer); let correct=0,wrong=0,skipped=0; active.forEach((q,idx)=>{ if(answers[idx]===undefined) skipped++; else if(Number(answers[idx])===Number(q.correct)) correct++; else wrong++; }); let score=Math.round(((correct*1)-(wrong*NEGATIVE_MARK))*100)/100; if(Object.is(score,-0)) score=0; const attempted=Math.max(1,correct+wrong); const accuracy=((correct/attempted)*100).toFixed(2)+'%'; const percentage=((correct/Math.max(1,active.length))*100).toFixed(2)+'%'; const usedSeconds=totalSeconds-leftSeconds; $('resultName').textContent=`${$('studentName').value.trim()||'Student'} — __TITLE_TEXT__`; $('resultScore').textContent=score.toFixed(2); buildSummaryCards([{label:'Correct',value:correct},{label:'Wrong',value:wrong},{label:'Skipped',value:skipped},{label:'Negative / wrong',value:NEGATIVE_MARK.toFixed(2)},{label:'Accuracy',value:accuracy},{label:'Percentage',value:percentage},{label:'Time used',value:fmt(usedSeconds)},{label:'Questions',value:active.length}]); const sectionMap={}; active.forEach((q,idx)=>{ if(!sectionMap[q.section]) sectionMap[q.section]={total:0,correct:0,wrong:0,skipped:0}; sectionMap[q.section].total+=1; if(answers[idx]===undefined) sectionMap[q.section].skipped+=1; else if(Number(answers[idx])===Number(q.correct)) sectionMap[q.section].correct+=1; else sectionMap[q.section].wrong+=1; }); $('sectionResultGrid').innerHTML=Object.entries(sectionMap).map(([name,item])=>{ const pct=item.total?Math.round((item.correct/item.total)*100):0; return `<div class='stat'><div class='label'>${name}</div><div class='value'>${item.correct}/${item.total}</div><div class='muted'>Wrong ${item.wrong} • Skipped ${item.skipped}</div><div class='bar'><span style='width:${pct}%'></span></div></div>`; }).join(''); const review=active.map((q,idx)=>{ const ans=answers[idx]; const status=ans===undefined?'skipped':(Number(ans)===Number(q.correct)?'correct':'wrong'); const chosen=ans===undefined?{raw:'Skipped',pretty:'Skipped',has_math:false}:q.options[ans]; const correctOpt=q.options[q.correct]; return {q_no:idx+1, section:q.section, status, question_raw:q.question_raw, question_pretty:q.question_pretty, question_has_math:q.question_has_math, question_image:q.question_image, answer_raw:chosen.raw, answer_pretty:chosen.pretty, answer_has_math:!!chosen.has_math, correct_raw:correctOpt.raw, correct_pretty:correctOpt.pretty, correct_has_math:!!correctOpt.has_math, explanation_raw:q.explanation_raw, explanation_pretty:q.explanation_pretty, explanation_has_math:q.explanation_has_math}; }).map(reviewBlock).join(''); $('reviewList').innerHTML=review; showPage('resultPage'); window.scrollTo({top:0,behavior:'smooth'}); typesetMath(); }
function applyFilter(mode){ document.querySelectorAll('.tab').forEach(btn=>btn.classList.toggle('active', btn.dataset.filter===mode)); document.querySelectorAll('.review-card').forEach(card=>{ card.style.display=(mode==='all'||card.dataset.status===mode)?'':'none'; }); }
document.querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>applyFilter(btn.dataset.filter)); $('startBtn').onclick=()=>{ renderExam(); showPage('examPage'); window.scrollTo({top:0,behavior:'smooth'}); startTimer(); }; $('submitBtn').onclick=finishExam; document.addEventListener('scroll',()=>{ if(!$('examPage').classList.contains('active')) return; const cards=[...document.querySelectorAll('.question-card')]; let activeIdx=0; for(const [idx,card] of cards.entries()){ const rect=card.getBoundingClientRect(); if(rect.top<=120) activeIdx=idx; } currentQuestion=activeIdx; updatePalette(); },{passive:true}); buildSectionSelectors(); typesetMath();
</script></body></html>"""
    html = (tpl.replace('__TITLE__', title_html)
              .replace('__TITLE_TEXT__', title_text)
              .replace('__QCOUNT__', str(len(questions)))
              .replace('__QTIME__', str(int(draft['question_time'])))
              .replace('__NEG__', str(draft['negative_mark']))
              .replace('__NEG_FLOAT__', str(float(draft['negative_mark'])))
              .replace('__DATA__', json.dumps(export_rows, ensure_ascii=False))
              .replace('__SECTIONS__', json.dumps(sections, ensure_ascii=False))
              .replace('__ACCENT__', theme['accent'])
              .replace('__ACCENT_SOFT__', theme['accent_soft'])
              .replace('__DANGER__', theme['danger'])
              .replace('__SUCCESS__', theme['success'])
              .replace('__WARNING__', theme['warning'])
              .replace('__LIGHT_BG__', theme['light_bg'])
              .replace('__LIGHT_TEXT__', theme['light_text'])
              .replace('__LIGHT_CARD__', theme['light_card'])
              .replace('__LIGHT_BORDER__', theme['light_border'])
              .replace('__LIGHT_MUTED__', theme['light_muted'])
              .replace('__DARK_BG__', theme['dark_bg'])
              .replace('__DARK_TEXT__', theme['dark_text'])
              .replace('__DARK_MUTED__', theme['dark_muted'])
              .replace('__DARK_SURFACE__', theme['dark_surface'])
              .replace('__DARK_SURFACE_2__', theme['dark_surface_2'])
              .replace('__DARK_BORDER__', theme['dark_border'])
              .replace('__DEEP_BG__', theme['deep_bg'])
              .replace('__DEFAULT_MODE__', theme['default_mode']))
    return html


def render_user_result_html(session: Any, participant_row: Any, rank_item: Dict[str, Any], ranking: List[Dict[str, Any]], review_items: List[Dict[str, Any]], section_items: List[Dict[str, Any]]) -> str:
    theme = _export_theme_palette(int(session['created_by']), {'html_export_theme': 'dark'})
    total_users = max(1, len(ranking))
    total_questions = int(session['total_questions'])
    correct = int(rank_item['correct'])
    wrong = int(rank_item['wrong'])
    skipped = int(rank_item['skipped'])
    attempted = max(1, correct + wrong)
    accuracy = (correct / attempted) * 100.0
    percentage = (correct / max(1, total_questions)) * 100.0
    percentile = 100.0 if total_users <= 1 else ((total_users - int(rank_item['rank'])) / (total_users - 1)) * 100.0
    score = str(rank_item['score'])
    title = base.html_escape(base.normalize_visual_text(session['title']))
    name = base.html_escape(base.normalize_visual_text(participant_row['display_name'] or 'Student'))
    summary_html = ''.join([
        f"<div class='stat'><div class='label'>Rank</div><div class='value'>#{rank_item['rank']}/{total_users}</div></div>",
        f"<div class='stat'><div class='label'>Score</div><div class='value'>{score}</div></div>",
        f"<div class='stat'><div class='label'>Accuracy</div><div class='value'>{accuracy:.2f}%</div></div>",
        f"<div class='stat'><div class='label'>Percentage</div><div class='value'>{percentage:.2f}%</div></div>",
        f"<div class='stat'><div class='label'>Percentile</div><div class='value'>{percentile:.2f}</div></div>",
        f"<div class='stat'><div class='label'>Negative / wrong</div><div class='value'>{session['negative_mark']}</div></div>",
    ])
    top_rows = []
    for item in ranking[:15]:
        current = ' class="me"' if int(item['user_id']) == int(participant_row['user_id']) else ''
        display = base.html_escape(item['name'] + (f" {item['sub_name']}" if item.get('sub_name') else ''))
        top_rows.append(f"<tr{current}><td>{item['rank']}</td><td>{display}</td><td>{item['correct']}</td><td>{item['wrong']}</td><td>{item['skipped']}</td><td>{base.html_escape(str(item['score']))}</td></tr>")
    section_cards = []
    for sec in section_items:
        pct = 0 if not sec['total'] else round((sec['correct'] / sec['total']) * 100)
        section_cards.append(f"<div class='stat'><div class='label'>{base.html_escape(sec['title'])}</div><div class='value'>{sec['correct']}/{sec['total']}</div><div class='muted'>Wrong {sec['wrong']} • Skipped {sec['skipped']}</div><div class='bar'><span style='width:{pct}%'></span></div></div>")
    review_cards = []
    for item in review_items:
        status = item['status']
        q_has_math = _contains_math_markup(item['question'])
        q_block = f"<div class='review-q math'>{_mathjax_html(item['question'])}</div>" if q_has_math else f"<div class='review-q'>{_html_from_display_text(item['question'])}</div>"
        chosen = str(item['chosen'])
        correct_opt = str(item['correct'])
        chosen_html = f"<span class='math'>{_mathjax_html(chosen)}</span>" if _contains_math_markup(chosen) else base.html_escape(_latex_to_pretty_text(chosen))
        correct_html = f"<span class='math'>{_mathjax_html(correct_opt)}</span>" if _contains_math_markup(correct_opt) else base.html_escape(_latex_to_pretty_text(correct_opt))
        exp = str(item.get('explanation') or '')
        if exp:
            if _contains_math_markup(exp):
                exp_html = f"<div class='line muted'><b>Explanation:</b> <span class='math'>{_mathjax_html(exp)}</span></div>"
            else:
                exp_html = f"<div class='line muted'><b>Explanation:</b> {_html_from_display_text(exp)}</div>"
        else:
            exp_html = ''
        review_cards.append(
            f"<div class='review-card {status}'><div class='head'><div><b>Q{item['q_no']}</b> • {base.html_escape(item['section'])}</div><div>{base.html_escape(status.title())}</div></div>"
            f"{q_block}<div class='line'><b>Your answer:</b> {chosen_html}</div><div class='line'><b>Correct answer:</b> {correct_html}</div>{exp_html}</div>"
        )
    tpl = """<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>__TITLE__ — Result</title><style>
:root{--accent:__ACCENT__;--success:__SUCCESS__;--danger:__DANGER__;--warning:__WARNING__;--bg:__LIGHT_BG__;--text:__LIGHT_TEXT__;--muted:__LIGHT_MUTED__;--surface:__LIGHT_CARD__;--border:__LIGHT_BORDER__}body{margin:0;font-family:Inter,system-ui,-apple-system,'Segoe UI',Roboto,Arial,'Noto Sans Bengali',sans-serif;background:var(--bg);color:var(--text)}.shell{width:min(1140px,100% - 24px);margin-inline:auto;padding:28px 0}.card{background:var(--surface);border:1px solid var(--border);border-radius:24px;box-shadow:0 18px 48px rgba(15,23,42,.12)}.hero{padding:26px}.title{font-size:clamp(28px,4vw,42px);font-weight:900}.name{color:var(--muted);margin-top:6px}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:18px}.stat{padding:18px;border-radius:18px;background:#f8fafc;border:1px solid var(--border)}.label{font-size:13px;color:var(--muted);font-weight:800}.value{font-size:clamp(30px,4vw,44px);font-weight:900;margin-top:8px}.two{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;margin-top:18px}.panel{padding:22px}.table{width:100%;border-collapse:separate;border-spacing:0 10px}.table th,.table td{padding:12px 14px;text-align:left}.table thead th{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.table tbody tr{background:#f8fafc}.table tbody tr.me{outline:2px solid rgba(37,99,235,.18)}.table tbody td:first-child{border-top-left-radius:14px;border-bottom-left-radius:14px}.table tbody td:last-child{border-top-right-radius:14px;border-bottom-right-radius:14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.bar{height:10px;border-radius:999px;background:#e5e7eb;overflow:hidden;margin-top:10px}.bar span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),#0f172a)}.reviews{display:grid;gap:14px}.review-card{padding:18px;border-radius:18px;background:#f8fafc;border:1px solid var(--border)}.review-card.correct{border-left:5px solid var(--success)}.review-card.wrong{border-left:5px solid var(--danger)}.review-card.skipped{border-left:5px solid var(--warning)}.head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.review-q{font-size:18px;line-height:1.6;margin:10px 0}.line{margin-top:8px;line-height:1.5}.muted{color:var(--muted)}@media(max-width:900px){.two{grid-template-columns:1fr}}</style><script>window.MathJax={tex:{inlineMath:[["\\(","\\)"],["$","$"]],displayMath:[["\\[","\\]"],["$$","$$"]]},svg:{fontCache:'global'}};</script><script defer src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js'></script></head><body><div class='shell'><div class='card hero'><div class='title'>__TITLE__</div><div class='name'>Professional result report for __NAME__</div><div class='summary'>__SUMMARY__</div></div><div class='two'><div class='card panel'><div class='title' style='font-size:24px'>Ranking Board</div><table class='table'><thead><tr><th>#</th><th>Name</th><th>Correct</th><th>Wrong</th><th>Skipped</th><th>Score</th></tr></thead><tbody>__TOP_ROWS__</tbody></table></div><div class='card panel'><div class='title' style='font-size:24px'>Section Analysis</div><div class='grid' style='margin-top:14px'>__SECTION_CARDS__</div></div></div><div class='card panel' style='margin-top:18px'><div class='title' style='font-size:24px'>Detailed Review</div><div class='reviews' style='margin-top:14px'>__REVIEWS__</div></div></div></body></html>"""
    return (tpl.replace('__TITLE__', title)
              .replace('__NAME__', name)
              .replace('__SUMMARY__', summary_html)
              .replace('__TOP_ROWS__', ''.join(top_rows))
              .replace('__SECTION_CARDS__', ''.join(section_cards))
              .replace('__REVIEWS__', ''.join(review_cards))
              .replace('__ACCENT__', theme['accent'])
              .replace('__SUCCESS__', theme['success'])
              .replace('__DANGER__', theme['danger'])
              .replace('__WARNING__', theme['warning'])
              .replace('__LIGHT_BG__', theme['light_bg'])
              .replace('__LIGHT_TEXT__', theme['light_text'])
              .replace('__LIGHT_CARD__', theme['light_card'])
              .replace('__LIGHT_BORDER__', theme['light_border'])
              .replace('__LIGHT_MUTED__', theme['light_muted']))
