"""Best-effort terminal-job notifications to an optional Slack/Discord-style webhook."""
import logging

import requests

from . import db

log = logging.getLogger("shelf.notify")


def terminal(job: dict, status: str):
    url = (db.settings().get("notify_webhook") or "").strip()
    if not url:
        return
    title = ""
    if job.get("item_id"):
        rows = db.q("SELECT title FROM items WHERE id=?", job["item_id"])
        title = rows[0]["title"] if rows else ""
    text = f"Shelf: {job.get('kind', 'job')} #{job.get('id')} {status}"
    if title:
        text += f" — {title}"
    if job.get("message"):
        text += f": {job['message']}"
    try:
        requests.post(url, json={"text": text, "content": text}, timeout=5)
    except requests.RequestException as e:
        log.warning("notify webhook failed: %s", e)
