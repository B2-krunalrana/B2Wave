"""
Email via SMTP (Gmail, Outlook, any provider) for campaign / bulk-SMS reports.
Uses only the Python standard library. Settings live in data/config.json -> "smtp".
"""
from __future__ import annotations

import asyncio
import html
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

PRESETS = {
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "outlook": {"host": "smtp.office365.com", "port": 587},
    "zoho": {"host": "smtp.zoho.in", "port": 587},
    "yahoo": {"host": "smtp.mail.yahoo.com", "port": 587},
}


def configured(cfg: dict | None) -> bool:
    return bool(cfg and cfg.get("host") and cfg.get("user") and cfg.get("password"))


def parse_recipients(s) -> list[str]:
    if isinstance(s, list):
        s = ",".join(s)
    out = []
    for part in (s or "").replace(";", ",").replace("\n", ",").split(","):
        p = part.strip()
        if p and "@" in p and p not in out:
            out.append(p)
    return out


def _send(cfg: dict, to: list[str], subject: str, html_body: str, text_body: str, attachments):
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "B2Wave", cfg["user"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    host, port = cfg.get("host", "smtp.gmail.com"), int(cfg.get("port") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def friendly(e: Exception) -> str:
    t = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "535" in t or "Username and Password not accepted" in t:
        return ("The mail server rejected the login. For Gmail, turn on 2-Step Verification and use a "
                "16-character App Password (myaccount.google.com/apppasswords), not your normal password.")
    if isinstance(e, (TimeoutError, OSError)) and ("timed out" in t or "getaddrinfo" in t or "10060" in t or "11001" in t):
        return "Could not reach the mail server. Check the internet connection, host and port."
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The recipient address was refused. Check the email address."
    return t or e.__class__.__name__


async def send(cfg: dict, to, subject: str, html_body: str, text_body: str = "", attachments=None):
    to = parse_recipients(to)
    if not configured(cfg):
        raise RuntimeError("Email is not set up. Open Settings > Email (SMTP).")
    if not to:
        raise RuntimeError("No valid recipient email address.")
    try:
        await asyncio.to_thread(_send, cfg, to, subject, html_body, text_body or subject, attachments)
    except Exception as e:
        raise RuntimeError(friendly(e)) from e
    return to


# ---------------------------------------------------------------- report templates
def _shell(title: str, inner: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;background:#f4f6fb;font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6fb;padding:24px 0"><tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #e4e8f0;border-radius:14px;overflow:hidden">
<tr><td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);background-color:#4f46e5;padding:20px 24px;color:#fff">
<div style="font-size:13px;opacity:.85">B2Wave report</div><div style="font-size:20px;font-weight:700">{html.escape(title)}</div></td></tr>
<tr><td style="padding:20px 24px">{inner}</td></tr>
<tr><td style="padding:14px 24px;border-top:1px solid #e4e8f0;color:#64748b;font-size:12px">Sent automatically by B2Wave. Full details are in the attached CSV.</td></tr>
</table></td></tr></table></body></html>"""


def _kpis(items) -> str:
    cells = "".join(f"""<td style="padding:6px"><div style="background:#f1f4f9;border-radius:10px;padding:12px">
<div style="font-size:12px;color:#64748b">{html.escape(k)}</div><div style="font-size:20px;font-weight:700">{html.escape(str(v))}</div></div></td>"""
                    for k, v in items)
    return f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table>'


def _table(head, rows) -> str:
    th = "".join(f'<th align="left" style="padding:8px;border-bottom:1px solid #e4e8f0;font-size:12px;color:#64748b">{html.escape(h)}</th>' for h in head)
    tr = "".join("<tr>" + "".join(f'<td style="padding:8px;border-bottom:1px solid #f1f4f9;font-size:13px">{html.escape(str(c))}</td>' for c in r) + "</tr>" for r in rows)
    return f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px">{"<tr>" + th + "</tr>"}{tr}</table>'


def campaign_report(camp: dict) -> tuple[str, str, str]:
    sm = camp.get("summary") or {}
    subject = f"B2Wave campaign finished: {camp['name']} ({sm.get('answered', 0)}/{sm.get('called', 0)} answered)"
    out = "".join(f"<li>{html.escape(k)}: <b>{v}</b></li>" for k, v in sorted((sm.get("outcomes") or {}).items(), key=lambda x: -x[1]))
    keys = "".join(f"<li>Pressed <b>{html.escape(k)}</b>: {v}</li>" for k, v in sorted((sm.get("keys") or {}).items()))
    rows = [(r["number"], r.get("outcome") or r.get("status"), r.get("keys") or "-",
             f"{r['ring_s']} s" if r.get("ring_s") is not None else "-", f"{r.get('talk_s', 0)} s", r.get("cause", ""))
            for r in camp["results"][:200]]
    inner = (f'<p style="margin:0 0 12px">Campaign <b>{html.escape(camp["name"])}</b> ended with status <b>{html.escape(camp["status"])}</b>.</p>'
             + _kpis([("Called", sm.get("called", 0)), ("Answered", f"{sm.get('answered', 0)} ({sm.get('answer_rate', 0)}%)"),
                      ("Responded", sm.get("responded", 0)), ("Avg talk", f"{sm.get('avg_talk_s', 0)} s")])
             + (f'<h3 style="font-size:15px;margin:18px 0 6px">Outcomes</h3><ul style="margin:0;padding-left:18px">{out}</ul>' if out else "")
             + (f'<h3 style="font-size:15px;margin:18px 0 6px">Key presses</h3><ul style="margin:0;padding-left:18px">{keys}</ul>' if keys else "")
             + _table(["Number", "Outcome", "Keys", "Ring", "Talk", "Network reason"], rows))
    text = f"{subject}\n" + "\n".join(f"{r[0]}: {r[1]} keys={r[2]}" for r in rows)
    return subject, _shell(f"Campaign: {camp['name']}", inner), text


def bulk_report(job: dict) -> tuple[str, str, str]:
    sent = sum(1 for r in job["results"] if r["status"] == "sent")
    failed = sum(1 for r in job["results"] if r["status"] == "failed")
    subject = f"B2Wave bulk SMS finished: {job['name']} ({sent} sent, {failed} failed)"
    rows = [(r["number"], r["status"], r.get("error", ""), r.get("time", "")[11:19]) for r in job["results"][:300]]
    inner = (f'<p style="margin:0 0 12px">Bulk SMS <b>{html.escape(job["name"])}</b> ended with status <b>{html.escape(job["status"])}</b>.</p>'
             + _kpis([("Recipients", len(job["results"])), ("Sent", sent), ("Failed", failed)])
             + f'<h3 style="font-size:15px;margin:18px 0 6px">Message</h3><div style="background:#f1f4f9;border-radius:10px;padding:12px;white-space:pre-wrap;font-size:14px">{html.escape(job["template"])}</div>'
             + _table(["Number", "Status", "Error", "Time"], rows))
    text = f"{subject}\n" + "\n".join(f"{r[0]}: {r[1]} {r[2]}" for r in rows)
    return subject, _shell(f"Bulk SMS: {job['name']}", inner), text
