#!/usr/bin/env python3
"""Send the open personal task list by Gmail SMTP."""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
TASKS_FILE = Path(__file__).resolve().parent.parent / "data" / "pending_tasks.json"
OPEN_STATUSES = {"pending", "in_progress", "waiting"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def load_tasks() -> list[dict]:
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"No se pudo leer la lista de pendientes: {exc}") from exc

    tasks = [
        task for task in data.get("tasks", [])
        if isinstance(task, dict) and task.get("status", "pending") in OPEN_STATUSES
    ]
    tasks.sort(key=lambda task: (
        task.get("target_date") is None,
        task.get("target_date") or "9999-12-31",
        PRIORITY_ORDER.get(task.get("priority", "medium"), 1),
    ))
    return tasks


def target_label(task: dict) -> str:
    date_value = task.get("target_date")
    if date_value:
        try:
            date_value = datetime.strptime(date_value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            pass
        return f"Fecha objetivo: {date_value}"
    return str(task.get("window") or "Sin fecha definida")


def build_message(tasks: list[dict], recipient: str) -> EmailMessage:
    now = datetime.now(BERLIN)
    msg = EmailMessage()
    msg["Subject"] = f"Pendientes — {now.strftime('%d/%m/%Y')} ({len(tasks)} abiertos)"
    msg["To"] = recipient

    if tasks:
        text_blocks = []
        for task in tasks:
            text_blocks.append(
                f"[{task.get('priority', 'medium').upper()}] {task.get('title', 'Tarea sin título')}\n"
                f"{task.get('category', 'General')} · {target_label(task)}\n"
                f"Siguiente paso: {task.get('next_action', 'Revisar tarea')}\n"
                f"ID: {task.get('id', 'sin-id')}"
            )
        plain = "\n\n".join(text_blocks)
    else:
        plain = "No tienes tareas pendientes registradas."

    msg.set_content(
        f"Pendientes abiertos ({len(tasks)})\n"
        f"{now.strftime('%d/%m/%Y %H:%M')} · Hora de Alemania\n\n"
        f"{plain}\n\n"
        "Esta lista se mantiene en GitHub: "
        "https://github.com/Ivangb-626/resumen-diario/blob/main/data/pending_tasks.json"
    )

    cards = []
    for task in tasks:
        priority = str(task.get("priority", "medium")).upper()
        cards.append(
            '<article style="padding:15px 0;border-bottom:1px solid #e5e7eb">'
            '<div style="font-size:12px;color:#64748b;margin-bottom:5px">'
            + html.escape(str(task.get("category", "General")))
            + " · " + html.escape(priority)
            + " · " + html.escape(target_label(task))
            + "</div><h2 style=\"font-size:17px;margin:0 0 7px\">"
            + html.escape(str(task.get("title", "Tarea sin título")))
            + "</h2><p style=\"font-size:14px;line-height:1.55;color:#334155;margin:0\">"
            + "Siguiente paso: " + html.escape(str(task.get("next_action", "Revisar tarea")))
            + "</p><p style=\"font-size:11px;color:#94a3b8;margin:7px 0 0\">ID: "
            + html.escape(str(task.get("id", "sin-id")))
            + "</p></article>"
        )

    body = (
        '<main style="max-width:680px;margin:24px auto;padding:26px;background:#fff;'
        'border:1px solid #e2e8f0;border-radius:12px;font-family:Arial,Helvetica,sans-serif;color:#0f172a">'
        '<p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64748b">Organización personal</p>'
        '<h1 style="font-size:25px;margin:0 0 6px">Tus pendientes</h1>'
        '<p style="font-size:14px;color:#64748b;margin:0 0 16px">'
        + now.strftime("%d/%m/%Y") + " · " + str(len(tasks)) + " tareas abiertas</p>"
        + ("".join(cards) if cards else "<p>No tienes tareas pendientes registradas.</p>")
        + '<p style="font-size:12px;color:#64748b;margin-top:22px">La lista editable está en '
        '<a href="https://github.com/Ivangb-626/resumen-diario/blob/main/data/pending_tasks.json">'
        'GitHub · pending_tasks.json</a>.</p></main>'
    )
    msg.add_alternative(body, subtype="html")
    return msg


def main() -> int:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    recipient = os.environ.get("GMAIL_RECIPIENT", "").strip() or sender
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not sender or not app_password or "@" not in sender or "@" not in recipient:
        print("Configura GMAIL_ADDRESS, GMAIL_APP_PASSWORD y, opcionalmente, GMAIL_RECIPIENT.", file=sys.stderr)
        return 2

    tasks = load_tasks()
    message = build_message(tasks, recipient)
    message["From"] = sender

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(sender, app_password.replace(" ", ""))
        server.send_message(message)

    print(f"Correo de pendientes enviado correctamente. Tareas abiertas: {len(tasks)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
