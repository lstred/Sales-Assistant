"""SMTP (send) + IMAP (receive) email client.

Outbound sending is gated by ``EmailConfig.enable_outbound_send`` so the app
ships in *manual review* mode by default — nothing leaves the machine until
the manager flips the toggle.
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes as _msg_from_bytes
from email.header import decode_header as _decode_header
from email.message import EmailMessage
from email.utils import make_msgid

from app.config.models import EmailConfig
from app.config.store import get_secret

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""


class EmailClient:
    def __init__(self, cfg: EmailConfig) -> None:
        self.cfg = cfg

    # --------------------------------------------------------------- SMTP send
    def send(
        self,
        *,
        to_address: str,
        subject: str,
        body_text: str,
        body_html: str = "",
        cc_address: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> SendResult:
        if not self.cfg.enable_outbound_send:
            return SendResult(
                ok=False,
                error="Outbound email is disabled in settings (manual review mode).",
            )
        if not self.cfg.smtp_host or not self.cfg.smtp_username:
            return SendResult(ok=False, error="SMTP host/username not configured.")

        password = get_secret("SMTP", self.cfg.smtp_username)
        if not password:
            return SendResult(ok=False, error="SMTP password not stored in keyring.")

        recipient = self.cfg.redirect_all_to or to_address

        msg = EmailMessage()
        msg["From"] = (
            f"{self.cfg.smtp_from_name} <{self.cfg.smtp_from_address}>"
            if self.cfg.smtp_from_name
            else self.cfg.smtp_from_address
        )
        msg["To"] = recipient
        if cc_address:
            msg["Cc"] = cc_address
        msg["Subject"] = subject
        message_id = make_msgid(domain=(self.cfg.smtp_from_address.split("@", 1)[-1] or "salesassistant.local"))
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip()

        msg.set_content(body_text or " ")
        if body_html:
            msg.add_alternative(body_html, subtype="html")

        recipients = [recipient]
        if cc_address:
            recipients.append(cc_address)

        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as s:
                s.ehlo()
                if self.cfg.smtp_starttls:
                    s.starttls(context=ctx)
                    s.ehlo()
                s.login(self.cfg.smtp_username, password)
                s.send_message(msg, to_addrs=recipients)
            return SendResult(ok=True, message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("SMTP send failed")
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    # --------------------------------------------------------------- SMTP test
    def test_smtp(self) -> tuple[bool, str]:
        password = get_secret("SMTP", self.cfg.smtp_username) if self.cfg.smtp_username else None
        if not self.cfg.smtp_host:
            return False, "SMTP host not set."
        if not password:
            return False, "SMTP password not stored in keyring."
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
                s.ehlo()
                if self.cfg.smtp_starttls:
                    s.starttls(context=ctx)
                    s.ehlo()
                s.login(self.cfg.smtp_username, password)
            return True, f"SMTP login OK ({self.cfg.smtp_host}:{self.cfg.smtp_port})."
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    # --------------------------------------------------------------- IMAP poll
    def fetch_new_replies(self) -> list[dict]:
        """Fetch unseen messages from the IMAP inbox.

        Returns a list of message dicts (one per message) with keys:
            message_id, in_reply_to, references, from_address,
            subject, body_text, body_html, imap_uid.

        Messages are NOT marked as seen — the caller decides.
        Returns [] when IMAP is not configured or the fetch fails.
        """
        password = get_secret("IMAP", self.cfg.imap_username) if self.cfg.imap_username else None
        if not self.cfg.imap_host or not password:
            return []

        results: list[dict] = []
        try:
            cls = imaplib.IMAP4_SSL if self.cfg.imap_ssl else imaplib.IMAP4
            with cls(self.cfg.imap_host, self.cfg.imap_port) as imap:
                imap.login(self.cfg.imap_username, password)
                imap.select(self.cfg.imap_mailbox or "INBOX")

                # BODY.PEEK[] fetches the full message without setting \Seen.
                _, data = imap.uid("SEARCH", None, "UNSEEN")
                uids = data[0].split() if data[0] else []

                for uid in uids:
                    try:
                        _, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                        if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                            continue
                        raw = msg_data[0][1]
                        msg = _msg_from_bytes(raw)

                        in_reply_to = msg.get("In-Reply-To", "").strip()
                        references = msg.get("References", "").strip()
                        message_id = msg.get("Message-ID", "").strip()
                        from_raw = msg.get("From", "").strip()

                        subject_raw = msg.get("Subject", "")
                        try:
                            subject = "".join(
                                part.decode(enc or "utf-8", errors="replace")
                                if isinstance(part, bytes) else part
                                for part, enc in _decode_header(subject_raw)
                            )
                        except Exception:
                            subject = subject_raw

                        body_text, body_html = self._extract_body(msg)

                        results.append({
                            "message_id": message_id,
                            "in_reply_to": in_reply_to,
                            "references": references,
                            "from_address": from_raw,
                            "subject": subject,
                            "body_text": body_text,
                            "body_html": body_html,
                            "imap_uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                        })
                    except Exception as exc:
                        log.warning("Failed to process IMAP uid=%s: %s", uid, exc)
        except Exception as exc:
            log.warning("IMAP fetch_new_replies failed: %s", exc)

        return results

    @staticmethod
    def _extract_body(msg) -> tuple[str, str]:
        """Return (body_text, body_html) from a parsed email.Message."""
        body_text = ""
        body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if "attachment" in str(part.get("Content-Disposition", "")):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
                if ct == "text/plain" and not body_text:
                    body_text = content
                elif ct == "text/html" and not body_html:
                    body_html = content
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html = content
                else:
                    body_text = content
        return body_text, body_html

    # --------------------------------------------------------------- IMAP test
    def test_imap(self) -> tuple[bool, str]:
        password = get_secret("IMAP", self.cfg.imap_username) if self.cfg.imap_username else None
        if not self.cfg.imap_host:
            return False, "IMAP host not set."
        if not password:
            return False, "IMAP password not stored in keyring."
        try:
            cls = imaplib.IMAP4_SSL if self.cfg.imap_ssl else imaplib.IMAP4
            with cls(self.cfg.imap_host, self.cfg.imap_port) as imap:
                imap.login(self.cfg.imap_username, password)
                imap.select(self.cfg.imap_mailbox, readonly=True)
            return True, f"IMAP login OK ({self.cfg.imap_host}:{self.cfg.imap_port})."
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
