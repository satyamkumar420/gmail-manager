#!/usr/bin/env python3
"""
Gmail Manager MCP Server for Antigravity (AGY)
High-performance, pure Gmail management MCP server providing:
1. read_inbox
2. get_email_details
3. search_emails
4. send_email
5. send_email_with_attachment
6. reply_to_email
7. create_draft
8. list_drafts
9. delete_email
10. clean_bounce_notifications
11. categorize_emails
12. generate_followup_template
13. manage_labels
14. get_mailbox_stats
15. verify_email_deliverability
"""

import os
import sys
import json
import time
import re
from datetime import datetime
import smtplib
import imaplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict, Any, Optional

try:
    import dns.resolver
except ImportError:
    dns = None

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("gmail-manager")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

def load_config() -> Dict[str, Any]:
    """Loads Gmail credentials and server host settings dynamically from environment or config.json.
    No credentials or sensitive values are hardcoded in source code."""
    config = {
        "sender_email": os.environ.get("GMAIL_SENDER_EMAIL", ""),
        "app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "imap_host": os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
        "imap_port": int(os.environ.get("GMAIL_IMAP_PORT", "993")),
        "smtp_host": os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("GMAIL_SMTP_PORT", "465")),
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                file_cfg = json.load(f)
                for k, v in file_cfg.items():
                    if not config.get(k):
                        config[k] = v
        except Exception:
            pass

    if not config.get("sender_email") or not config.get("app_password"):
        raise ValueError(
            "Missing credentials! Please set GMAIL_SENDER_EMAIL and GMAIL_APP_PASSWORD "
            "in your environment variables (mcp_config.json) or in config.json."
        )

    return config

def get_imap_client():
    """Connects and logs in to IMAP server."""
    cfg = load_config()
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    imap.login(cfg["sender_email"], cfg["app_password"])
    return imap

def format_imap_date(date_str: Optional[str]) -> Optional[str]:
    """Formats ISO or freeform date string (YYYY-MM-DD, DD-Mon-YYYY, etc.) to standard IMAP date format (DD-Mon-YYYY)."""
    if not date_str:
        return None
    cleaned = date_str.strip()
    # Check if already in DD-Mon-YYYY format (e.g. 04-Sep-2026 or 4-Sep-2026)
    match = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", cleaned)
    if match:
        day, month, year = match.groups()
        return f"{int(day):02d}-{month.capitalize()}-{year}"
        
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%d-%b-%Y")
        except ValueError:
            continue
    return None

def decode_mime_words(raw_header: str) -> str:
    """Decodes MIME encoded header strings into readable UTF-8 text."""
    if not raw_header:
        return ""
    decoded_parts = []
    for text, encoding in decode_header(raw_header):
        if isinstance(text, bytes):
            try:
                decoded_parts.append(text.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                decoded_parts.append(text.decode("latin1", errors="ignore"))
        else:
            decoded_parts.append(str(text))
    return "".join(decoded_parts)

def extract_body(msg: email.message.Message) -> Dict[str, str]:
    """Extracts text and HTML content from an email message."""
    text_content = ""
    html_content = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdispo = str(part.get("Content-Disposition"))
            if "attachment" in cdispo:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                decoded = payload.decode("utf-8", errors="ignore")
                if ctype == "text/plain" and not text_content:
                    text_content = decoded
                elif ctype == "text/html" and not html_content:
                    html_content = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode("utf-8", errors="ignore")
            if msg.get_content_type() == "text/html":
                html_content = decoded
            else:
                text_content = decoded
                
    return {
        "text": text_content.strip() or html_content.strip(),
        "html": html_content.strip()
    }

def check_email_deliverability(email_addr: str, sender_email: str = "", timeout: int = 8) -> Dict[str, Any]:
    """Performs deep pre-flight email deliverability validation:
    1. RFC syntax format check
    2. DNS MX records existence check
    3. Zero-send SMTP handshake with the target MX server (RCPT TO probe)
    4. Catch-all domain detection
    """
    cleaned_email = (email_addr or "").strip()
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(pattern, cleaned_email):
        return {
            "email": cleaned_email,
            "is_deliverable": False,
            "status": "invalid_syntax",
            "reason": "Malformed email address syntax."
        }

    domain = cleaned_email.split("@")[1].lower()

    # DNS MX check
    mxs = []
    if dns:
        try:
            answers = dns.resolver.resolve(domain, "MX")
            mx_records = sorted([(r.preference, str(r.exchange).rstrip(".")) for r in answers], key=lambda x: x[0])
            mxs = [m[1] for m in mx_records]
        except Exception as e:
            return {
                "email": cleaned_email,
                "domain": domain,
                "is_deliverable": False,
                "status": "no_mx_records",
                "reason": f"No valid MX records found for domain '{domain}': {str(e)}"
            }
    else:
        import subprocess
        try:
            out = subprocess.check_output(["dig", "+short", "MX", domain], timeout=5).decode()
            for line in out.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2:
                    mxs.append(parts[1].rstrip("."))
        except Exception:
            pass

    if not mxs:
        return {
            "email": cleaned_email,
            "domain": domain,
            "is_deliverable": False,
            "status": "no_mx_records",
            "reason": f"No valid MX records found for domain '{domain}'."
        }

    target_mx = mxs[0]
    is_catch_all = False

    try:
        server = smtplib.SMTP(target_mx, 25, timeout=timeout)
        server.ehlo_or_helo_if_needed()
        from_addr = sender_email if sender_email else "verify@gmail.com"
        server.mail(from_addr)
        code, msg = server.rcpt(cleaned_email)
        msg_str = msg.decode(errors="ignore").strip()

        # Hard rejection by recipient mail server (e.g. 550 User unknown)
        if code >= 500:
            server.quit()
            return {
                "email": cleaned_email,
                "domain": domain,
                "target_mx": target_mx,
                "is_deliverable": False,
                "status": "undeliverable",
                "smtp_code": code,
                "reason": f"Mail server rejected recipient with code {code}: {msg_str}"
            }

        # Catch-all detection
        if code == 250:
            try:
                server.rset()
                server.mail(from_addr)
                dummy_user = f"antigravity_verify_probe_{abs(hash(cleaned_email)) % 100000}@{domain}"
                dummy_code, _ = server.rcpt(dummy_user)
                if dummy_code == 250:
                    is_catch_all = True
            except Exception:
                pass

        server.quit()

        if is_catch_all:
            return {
                "email": cleaned_email,
                "domain": domain,
                "target_mx": target_mx,
                "is_deliverable": True,
                "is_catch_all": True,
                "status": "risky_catch_all",
                "smtp_code": 250,
                "reason": "Domain accepts all recipients (Catch-All). Domain active, individual mailbox existence cannot be strictly verified."
            }
        else:
            return {
                "email": cleaned_email,
                "domain": domain,
                "target_mx": target_mx,
                "is_deliverable": True,
                "is_catch_all": False,
                "status": "verified",
                "smtp_code": 250,
                "reason": "Mailbox confirmed active and individually deliverable by mail server."
            }
    except Exception as e:
        return {
            "email": cleaned_email,
            "domain": domain,
            "target_mx": target_mx,
            "is_deliverable": True,
            "is_catch_all": False,
            "status": "mx_verified_smtp_unreachable",
            "reason": f"MX record verified ({target_mx}) but direct SMTP handshake timed out or was refused: {str(e)}"
        }

# ==============================================================================
# 1. READ & INBOX TOOLS
# ==============================================================================

@mcp.tool()
def read_inbox(limit: int = 100, unread_only: bool = False, folder: str = "INBOX", since_date: Optional[str] = None) -> str:
    """Reads latest emails from Inbox or a specified folder.
    
    Args:
        limit: Number of emails to retrieve (default: 100, custom value supported up to 1000).
        unread_only: If True, fetches only unread messages (default: False).
        folder: Mailbox folder name (default: 'INBOX').
        since_date: Optional date filter (e.g. '2026-09-04' or '04-Sep-2026').
    """
    custom_limit = limit if limit and limit > 0 else 100
    limit = min(custom_limit, 1000)
    try:
        imap = get_imap_client()
        status, _ = imap.select(f'"{folder}"' if " " in folder else folder)
        if status != "OK":
            return json.dumps({"status": "error", "message": f"Folder {folder} not found."})
            
        criteria = []
        if unread_only:
            criteria.append("UNSEEN")
        if since_date:
            imap_date = format_imap_date(since_date)
            if imap_date:
                criteria.append(f"SINCE {imap_date}")
                
        search_criteria = " ".join(criteria) if criteria else "ALL"
        status, data = imap.search(None, search_criteria)
        if not data[0]:
            imap.logout()
            return json.dumps({"status": "success", "count": 0, "emails": []})
            
        msg_ids = data[0].split()
        target_ids = msg_ids[-limit:]
        target_ids.reverse() # Latest first
        
        # Batch fetch headers
        id_str = b",".join(target_ids).decode("utf-8")
        status, fetch_data = imap.fetch(id_str, "(BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
        
        emails_list = []
        if status == "OK" and fetch_data:
            for item in fetch_data:
                if isinstance(item, tuple):
                    header_msg = email.message_from_bytes(item[1])
                    mid_str = item[0].split()[0].decode("utf-8")
                    emails_list.append({
                        "id": mid_str,
                        "from": decode_mime_words(header_msg.get("From", "")),
                        "to": decode_mime_words(header_msg.get("To", "")),
                        "subject": decode_mime_words(header_msg.get("Subject", "")),
                        "date": header_msg.get("Date", "")
                    })
            emails_list.reverse()
                
        imap.logout()
        return json.dumps({"status": "success", "count": len(emails_list), "emails": emails_list}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def get_email_details(message_id: str, folder: str = "INBOX") -> str:
    """Retrieves full details, headers, body text, and attachments metadata of an email.
    
    Args:
        message_id: The numeric ID of the message.
        folder: Mailbox folder name (default: 'INBOX').
    """
    try:
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        
        status, fetch_data = imap.fetch(message_id.encode("utf-8"), "(BODY.PEEK[])")
        if status != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
            imap.logout()
            return json.dumps({"status": "error", "message": f"Message ID {message_id} not found."})
            
        msg = email.message_from_bytes(fetch_data[0][1])
        body_data = extract_body(msg)
        
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                cdispo = str(part.get("Content-Disposition"))
                if "attachment" in cdispo:
                    filename = part.get_filename()
                    if filename:
                        attachments.append(decode_mime_words(filename))
                        
        details = {
            "id": message_id,
            "message_id_header": msg.get("Message-ID", ""),
            "from": decode_mime_words(msg.get("From", "")),
            "to": decode_mime_words(msg.get("To", "")),
            "subject": decode_mime_words(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "attachments": attachments,
            "body": body_data["text"][:8000]
        }
        
        imap.logout()
        return json.dumps({"status": "success", "email": details}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def search_emails(query: str = "", folder: str = "INBOX", limit: int = 100, since_date: Optional[str] = None) -> str:
    """Searches emails by subject, sender, text query, or date range.
    
    Args:
        query: Search string or keyword (searches across Subject, From, Body). Default is empty string.
        folder: Mailbox folder name (default: 'INBOX').
        limit: Max results to return (default: 100, custom value supported up to 1000).
        since_date: Optional date filter (e.g. '2026-09-04' or '04-Sep-2026').
    """
    custom_limit = limit if limit and limit > 0 else 100
    limit = min(custom_limit, 1000)
    try:
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        
        safe_query = (query or "").replace('"', '').strip()
        
        date_clause = ""
        if since_date:
            imap_date = format_imap_date(since_date)
            if imap_date:
                date_clause = f" SINCE {imap_date}"
                
        if safe_query:
            search_query = f'(OR (OR FROM "{safe_query}" SUBJECT "{safe_query}") TEXT "{safe_query}"){date_clause}'
            status, data = imap.search(None, search_query)
            if status != "OK" or not data or not data[0]:
                search_query = f'SUBJECT "{safe_query}"{date_clause}'
                status, data = imap.search(None, search_query)
        elif date_clause:
            search_query = date_clause.strip()
            status, data = imap.search(None, search_query)
        else:
            status, data = imap.search(None, "ALL")
            
        if not data or not data[0]:
            imap.logout()
            return json.dumps({"status": "success", "count": 0, "emails": []})
            
        msg_ids = data[0].split()
        target_ids = msg_ids[-limit:]
        target_ids.reverse()
        
        # Batch fetch headers
        id_str = b",".join(target_ids).decode("utf-8")
        status, fetch_data = imap.fetch(id_str, "(BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
        
        results = []
        if status == "OK" and fetch_data:
            for item in fetch_data:
                if isinstance(item, tuple):
                    header_msg = email.message_from_bytes(item[1])
                    mid_str = item[0].split()[0].decode("utf-8")
                    results.append({
                        "id": mid_str,
                        "from": decode_mime_words(header_msg.get("From", "")),
                        "to": decode_mime_words(header_msg.get("To", "")),
                        "subject": decode_mime_words(header_msg.get("Subject", "")),
                        "date": header_msg.get("Date", "")
                    })
            results.reverse()
                
        imap.logout()
        return json.dumps({"status": "success", "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# ==============================================================================
# 2. SENDING & ATTACHMENT & THREAD REPLY TOOLS
# ==============================================================================

@mcp.tool()
def verify_email_deliverability(email_address: str, timeout: int = 8) -> str:
    """Performs deep pre-flight email deliverability verification before sending.
    
    Checks:
    1. RFC syntax format
    2. DNS MX records existence
    3. Zero-send SMTP handshake with the target MX server (RCPT TO probe)
    4. Catch-all domain detection
    
    Args:
        email_address: The target email address to verify.
        timeout: Socket timeout in seconds for SMTP probe (default: 8).
    """
    try:
        cfg = load_config()
        result = check_email_deliverability(email_address, cfg.get("sender_email", ""), timeout=timeout)
        return json.dumps({"status": "success", "deliverability": result}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def send_email(to: str, subject: str, body: str, cc: Optional[str] = None, bcc: Optional[str] = None, html_body: Optional[str] = None, verify_deliverability: bool = True) -> str:
    """Sends an email dynamically via SMTP with pre-send deliverability protection.
    
    Args:
        to: Recipient email address (or comma-separated addresses).
        subject: Dynamic subject line of the email.
        body: Plain text content of the email.
        cc: Optional CC email address.
        bcc: Optional BCC email address.
        html_body: Optional HTML formatted content of the email.
        verify_deliverability: If True (default), verifies mailbox deliverability before sending to prevent bounces.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            recipients.extend([r.strip() for r in cc.split(",") if r.strip()])
        if bcc:
            recipients.extend([r.strip() for r in bcc.split(",") if r.strip()])

        if verify_deliverability:
            for rec in recipients:
                ver_res = check_email_deliverability(rec, sender_email)
                if not ver_res["is_deliverable"]:
                    return json.dumps({
                        "status": "blocked",
                        "error_code": "PRE_SEND_VERIFICATION_FAILED",
                        "blocked_recipient": rec,
                        "verification_details": ver_res,
                        "message": f"Dispatch blocked for '{rec}': {ver_res['reason']}. Prevented bounce to protect sender reputation."
                    }, indent=2)
        
        msg = MIMEMultipart("alternative")
        msg["From"] = sender_email
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
            
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"])
        server.login(sender_email, cfg["app_password"])
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()
        
        return json.dumps({"status": "success", "message": f"Email successfully sent to {to}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def send_email_with_attachment(to: str, subject: str, body: str, file_path: str, cc: Optional[str] = None, html_body: Optional[str] = None, verify_deliverability: bool = True) -> str:
    """Sends an email with a file attachment dynamically with pre-send deliverability protection.
    
    Args:
        to: Recipient email address.
        subject: Dynamic subject line.
        body: Plain text email body.
        file_path: Local path to any file to attach.
        cc: Optional CC address.
        html_body: Optional HTML formatted email body.
        verify_deliverability: If True (default), verifies mailbox deliverability before sending to prevent bounces.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        if not os.path.exists(file_path):
            return json.dumps({"status": "error", "message": f"Attachment file not found at: {file_path}"})

        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            recipients.extend([r.strip() for r in cc.split(",") if r.strip()])

        if verify_deliverability:
            for rec in recipients:
                ver_res = check_email_deliverability(rec, sender_email)
                if not ver_res["is_deliverable"]:
                    return json.dumps({
                        "status": "blocked",
                        "error_code": "PRE_SEND_VERIFICATION_FAILED",
                        "blocked_recipient": rec,
                        "verification_details": ver_res,
                        "message": f"Dispatch blocked for '{rec}': {ver_res['reason']}. Prevented bounce to protect sender reputation."
                    }, indent=2)
            
        msg = MIMEMultipart("mixed")
        msg["From"] = sender_email
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
            
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            body_part.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(body_part)
        
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename= {filename}")
            msg.attach(part)
            
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"])
        server.login(sender_email, cfg["app_password"])
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()
        
        return json.dumps({"status": "success", "message": f"Email with attachment '{filename}' successfully sent to {to}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def reply_to_email(message_id: str, reply_body: str, folder: str = "INBOX", html_reply_body: Optional[str] = None) -> str:
    """Replies dynamically to an existing email thread, preserving In-Reply-To and References headers.
    
    Args:
        message_id: The numeric ID of the email to reply to.
        reply_body: Plain text content of your reply.
        folder: Folder where the original email is stored (default: 'INBOX').
        html_reply_body: Optional HTML formatted reply body.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        status, fetch_data = imap.fetch(message_id.encode("utf-8"), "(RFC822)")
        if status != "OK" or not fetch_data or not isinstance(fetch_data[0], tuple):
            imap.logout()
            return json.dumps({"status": "error", "message": f"Original message ID {message_id} not found."})
            
        orig_msg = email.message_from_bytes(fetch_data[0][1])
        imap.logout()
        
        orig_from = orig_msg.get("Reply-To") or orig_msg.get("From", "")
        orig_subject = decode_mime_words(orig_msg.get("Subject", ""))
        orig_msg_id = orig_msg.get("Message-ID", "")
        
        import re
        recipients = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', orig_from)
        to_address = recipients[0] if recipients else orig_from
        
        subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
        
        msg = MIMEMultipart("alternative")
        msg["From"] = sender_email
        msg["To"] = to_address
        msg["Subject"] = subject
        if orig_msg_id:
            msg["In-Reply-To"] = orig_msg_id
            msg["References"] = orig_msg_id
            
        msg.attach(MIMEText(reply_body, "plain", "utf-8"))
        if html_reply_body:
            msg.attach(MIMEText(html_reply_body, "html", "utf-8"))
        
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"])
        server.login(sender_email, cfg["app_password"])
        server.sendmail(sender_email, [to_address], msg.as_string())
        server.quit()
        
        return json.dumps({"status": "success", "message": f"Reply successfully sent to {to_address} (Thread: {subject})"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# ==============================================================================
# 3. DRAFT & DELETION TOOLS
# ==============================================================================

@mcp.tool()
def create_draft(to: str, subject: str, body: str, html_body: Optional[str] = None) -> str:
    """Creates a draft email dynamically in the Gmail Drafts folder.
    
    Args:
        to: Recipient email address.
        subject: Subject line of the draft.
        body: Plain text body of the draft.
        html_body: Optional HTML formatted body of the draft.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        msg = MIMEMultipart("alternative")
        msg["From"] = sender_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        
        imap = get_imap_client()
        raw_message = msg.as_bytes()
        imap.append('"[Gmail]/Drafts"', "\\Draft", imaplib.Time2Internaldate(time.time()), raw_message)
        imap.logout()
        
        return json.dumps({"status": "success", "message": f"Draft created for {to} in [Gmail]/Drafts"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def list_drafts(limit: int = 10) -> str:
    """Lists existing draft messages in the [Gmail]/Drafts folder.
    
    Args:
        limit: Maximum number of drafts to retrieve (default: 10).
    """
    try:
        imap = get_imap_client()
        status, _ = imap.select('"[Gmail]/Drafts"')
        if status != "OK":
            imap.logout()
            return json.dumps({"status": "error", "message": "Could not access [Gmail]/Drafts folder."})
            
        status, data = imap.search(None, "ALL")
        if not data[0]:
            imap.logout()
            return json.dumps({"status": "success", "count": 0, "drafts": []})
            
        msg_ids = data[0].split()
        target_ids = msg_ids[-limit:]
        target_ids.reverse()
        
        id_str = b",".join(target_ids).decode("utf-8")
        status, fetch_data = imap.fetch(id_str, "(BODY[HEADER.FIELDS (TO SUBJECT DATE)])")
        
        drafts = []
        if status == "OK" and fetch_data:
            idx = 0
            for item in fetch_data:
                if isinstance(item, tuple):
                    hdr = email.message_from_bytes(item[1])
                    mid_str = target_ids[idx].decode("utf-8") if idx < len(target_ids) else ""
                    idx += 1
                    drafts.append({
                        "id": mid_str,
                        "to": decode_mime_words(hdr.get("To", "")),
                        "subject": decode_mime_words(hdr.get("Subject", "")),
                        "date": hdr.get("Date", "")
                    })
                
        imap.logout()
        return json.dumps({"status": "success", "count": len(drafts), "drafts": drafts}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def delete_email(message_id: str, folder: str = "INBOX", move_to_trash: bool = True) -> str:
    """Deletes an email or moves it to Trash (supports single ID or comma-separated batch IDs).
    
    Args:
        message_id: Numeric message ID or comma-separated IDs (e.g. '123' or '101,102,103').
        folder: Mailbox folder where the email is located (default: 'INBOX').
        move_to_trash: If True (default), moves message to [Gmail]/Trash. If False, permanently deletes message.
    """
    try:
        imap = get_imap_client()
        folder_str = f'"{folder}"' if " " in folder else folder
        imap.select(folder_str)
        
        ids = [mid.strip() for mid in message_id.split(",") if mid.strip()]
        if not ids:
            imap.logout()
            return json.dumps({"status": "error", "message": "No valid message IDs provided."})
            
        deleted_count = 0
        for mid in ids:
            mid_bytes = mid.encode("utf-8")
            if move_to_trash and folder != "[Gmail]/Trash":
                # Copy to Trash folder and mark deleted in current folder
                try:
                    imap.copy(mid_bytes, '"[Gmail]/Trash"')
                except Exception:
                    pass
            imap.store(mid_bytes, "+FLAGS", r"(\Deleted)")
            deleted_count += 1
            
        imap.expunge()
        imap.logout()
        
        action_desc = "moved to Trash" if move_to_trash else "permanently deleted"
        return json.dumps({
            "status": "success",
            "count": deleted_count,
            "folder": folder,
            "message": f"{deleted_count} email(s) {action_desc} from {folder}."
        })
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def clean_bounce_notifications(folder: str = "INBOX") -> str:
    """Scans and removes automated failure / mailer-daemon bounce notifications.
    
    Args:
        folder: Mailbox folder to clean (default: 'INBOX').
    """
    try:
        imap = get_imap_client()
        folder_str = f'"{folder}"' if " " in folder else folder
        imap.select(folder_str)
        
        status, data = imap.search(None, 'FROM "mailer-daemon@googlemail.com"')
        if not data[0]:
            imap.logout()
            return json.dumps({"status": "success", "cleaned_count": 0, "message": "No bounce notifications found."})
            
        msg_ids = data[0].split()
        for mid in msg_ids:
            imap.store(mid, "+FLAGS", r"(\Deleted)")
            
        imap.expunge()
        imap.logout()
        return json.dumps({"status": "success", "cleaned_count": len(msg_ids), "message": f"Purged {len(msg_ids)} bounce notifications."})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# ==============================================================================
# 4. INTELLIGENCE & ANALYTICS TOOLS
# ==============================================================================

@mcp.tool()
def categorize_emails(limit: int = 15, folder: str = "INBOX") -> str:
    """Classifies recent inbox or folder messages into standard business categories.
    
    Args:
        limit: Number of recent emails to evaluate (default: 15).
        folder: Mailbox folder name (default: 'INBOX').
    """
    try:
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        
        status, data = imap.search(None, "ALL")
        if not data[0]:
            imap.logout()
            return json.dumps({"status": "success", "total_analyzed": 0, "categories": {}})
            
        msg_ids = data[0].split()
        target_ids = msg_ids[-limit:]
        target_ids.reverse()
        
        classified = {
            "action_required": [],
            "calendar_and_meetings": [],
            "receipts_and_orders": [],
            "updates_and_newsletters": [],
            "direct_conversations": []
        }
        
        for mid in target_ids:
            mid_str = mid.decode("utf-8")
            status, fetch_data = imap.fetch(mid, "(BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] BODY[TEXT]<0.1200>)")
            if status != "OK" or not fetch_data:
                continue
                
            header_text = ""
            body_snippet = ""
            for item in fetch_data:
                if isinstance(item, tuple):
                    if b"HEADER" in item[0]:
                        header_text = item[1].decode("utf-8", errors="ignore")
                    elif b"TEXT" in item[0]:
                        body_snippet = item[1].decode("utf-8", errors="ignore")
                        
            header_msg = email.message_from_string(header_text)
            subj = decode_mime_words(header_msg.get("Subject", ""))
            sender = decode_mime_words(header_msg.get("From", ""))
            date = header_msg.get("Date", "")
            
            combined_text = (subj + " " + body_snippet).lower()
            
            entry = {
                "id": mid_str,
                "from": sender,
                "subject": subj,
                "date": date
            }
            
            if any(k in combined_text for k in ["calendar", "meeting", "zoom.us", "meet.google.com", "teams.microsoft", "invite", "scheduled"]):
                classified["calendar_and_meetings"].append(entry)
            elif any(k in combined_text for k in ["urgent", "action required", "please confirm", "deadline", "review and submit", "verify your account"]):
                classified["action_required"].append(entry)
            elif any(k in combined_text for k in ["receipt", "invoice", "order confirmation", "payment received", "billing", "shipped"]):
                classified["receipts_and_orders"].append(entry)
            elif any(k in combined_text for k in ["newsletter", "unsubscribe", "weekly digest", "promotions", "no-reply", "noreply"]):
                classified["updates_and_newsletters"].append(entry)
            else:
                classified["direct_conversations"].append(entry)
                
        imap.logout()
        return json.dumps({"status": "success", "total_analyzed": len(target_ids), "categories": classified}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def generate_followup_template(original_subject_or_to: str, context_notes: Optional[str] = None, sender_name: Optional[str] = None) -> str:
    """Generates a professional, contextual follow-up email template based on a previously sent email or topic.
    
    Args:
        original_subject_or_to: Email address, company name, or subject line of previous email.
        context_notes: Specific points or notes to include in the follow-up (optional).
        sender_name: Your name to sign off with (optional).
    """
    try:
        cfg = load_config()
        s_name = sender_name or "Best regards"
        imap = get_imap_client()
        imap.select('"[Gmail]/Sent Mail"')
        
        status, data = imap.search(None, f'TEXT "{original_subject_or_to}"')
        found_subject = f"Following up: {original_subject_or_to}"
        recipient = original_subject_or_to
        
        if data[0]:
            last_id = data[0].split()[-1]
            status, fetch_data = imap.fetch(last_id, "(BODY[HEADER.FIELDS (TO SUBJECT)])")
            if status == "OK" and fetch_data and isinstance(fetch_data[0], tuple):
                hdr = email.message_from_bytes(fetch_data[0][1])
                orig_subj = decode_mime_words(hdr.get("Subject", ""))
                recipient = hdr.get("To", recipient)
                if orig_subj:
                    found_subject = f"Follow-up: {orig_subj}"
                    
        imap.logout()
        
        notes_line = f"\n\n{context_notes}" if context_notes else ""
        followup_body = f"""Hi,

I hope you are doing well.

I wanted to quickly follow up on my previous message regarding "{found_subject}". Please let me know if you have had a chance to review it or if you need any additional information.{notes_line}

Looking forward to hearing from you.

Sincerely,
{s_name}
{cfg.get('sender_email', '')}"""

        return json.dumps({
            "status": "success",
            "to": recipient,
            "suggested_subject": found_subject,
            "followup_body": followup_body.strip()
        }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def manage_labels(message_id: str, label_action: str = "star", label_name: Optional[str] = None, folder: str = "INBOX") -> str:
    """Manages Gmail labels and flags (e.g. Star, Read/Unread, Custom Labels).
    
    Args:
        message_id: The numeric ID of the message.
        label_action: Action to perform: 'star', 'unstar', 'mark_read', 'mark_unread', 'add_label'.
        label_name: Custom label name (if using 'add_label').
        folder: Folder where the email resides (default: 'INBOX').
    """
    try:
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        
        mid_bytes = message_id.encode("utf-8")
        if label_action == "star":
            imap.store(mid_bytes, "+FLAGS", r"(\Flagged)")
        elif label_action == "unstar":
            imap.store(mid_bytes, "-FLAGS", r"(\Flagged)")
        elif label_action == "mark_read":
            imap.store(mid_bytes, "+FLAGS", r"(\Seen)")
        elif label_action == "mark_unread":
            imap.store(mid_bytes, "-FLAGS", r"(\Seen)")
        elif label_action == "add_label" and label_name:
            imap.store(mid_bytes, "+X-GM-LABELS", f'"{label_name}"')
        else:
            imap.logout()
            return json.dumps({"status": "error", "message": f"Unsupported action: {label_action}"})
            
        imap.logout()
        return json.dumps({"status": "success", "message": f"Applied '{label_action}' to message ID {message_id}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def get_mailbox_stats() -> str:
    """Computes real-time Gmail mailbox statistics including Sent, Inbox, Unread, and Drafts counts.
    """
    try:
        imap = get_imap_client()
        
        imap.select('"[Gmail]/Sent Mail"')
        status, sent_data = imap.search(None, "ALL")
        total_sent = len(sent_data[0].split()) if sent_data[0] else 0
        
        imap.select("INBOX")
        status, all_inbox = imap.search(None, "ALL")
        total_inbox = len(all_inbox[0].split()) if all_inbox[0] else 0
        
        status, unseen_data = imap.search(None, "UNSEEN")
        unread_inbox = len(unseen_data[0].split()) if unseen_data[0] else 0
        
        imap.select('"[Gmail]/Drafts"')
        status, draft_data = imap.search(None, "ALL")
        total_drafts = len(draft_data[0].split()) if draft_data[0] else 0
        
        imap.logout()
        
        stats = {
            "total_sent_emails": total_sent,
            "total_inbox_messages": total_inbox,
            "unread_inbox_messages": unread_inbox,
            "total_draft_messages": total_drafts,
            "system_health": "Active & Connected (SSL 465/993)"
        }
        return json.dumps({"status": "success", "stats": stats}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
