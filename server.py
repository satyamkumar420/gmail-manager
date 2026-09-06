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
"""

import os
import sys
import json
import time
import smtplib
import imaplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict, Any, Optional

from mcp.server.mcpserver import MCPServer

# Initialize MCPServer
mcp = MCPServer("gmail-manager")

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

# ==============================================================================
# 1. READ & INBOX TOOLS
# ==============================================================================

@mcp.tool()
def read_inbox(limit: int = 10, unread_only: bool = False, folder: str = "INBOX") -> str:
    """Reads latest emails from Inbox or a specified folder.
    
    Args:
        limit: Number of emails to retrieve (default: 10, max: 50).
        unread_only: If True, fetches only unread messages (default: False).
        folder: Mailbox folder name (default: 'INBOX').
    """
    limit = min(max(1, limit), 50)
    try:
        imap = get_imap_client()
        status, _ = imap.select(f'"{folder}"' if " " in folder else folder)
        if status != "OK":
            return json.dumps({"status": "error", "message": f"Folder {folder} not found."})
            
        search_criteria = "UNSEEN" if unread_only else "ALL"
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
            idx = 0
            for item in fetch_data:
                if isinstance(item, tuple):
                    header_msg = email.message_from_bytes(item[1])
                    mid_str = target_ids[idx].decode("utf-8") if idx < len(target_ids) else ""
                    idx += 1
                    emails_list.append({
                        "id": mid_str,
                        "from": decode_mime_words(header_msg.get("From", "")),
                        "to": decode_mime_words(header_msg.get("To", "")),
                        "subject": decode_mime_words(header_msg.get("Subject", "")),
                        "date": header_msg.get("Date", "")
                    })
                
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
        
        status, fetch_data = imap.fetch(message_id.encode("utf-8"), "(RFC822)")
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
def search_emails(query: str, folder: str = "INBOX", limit: int = 10) -> str:
    """Searches emails by subject, sender, or text query.
    
    Args:
        query: Search string or keyword (searches across Subject, From, Body).
        folder: Mailbox folder name (default: 'INBOX').
        limit: Max results to return (default: 10).
    """
    try:
        imap = get_imap_client()
        imap.select(f'"{folder}"' if " " in folder else folder)
        
        # Search using OR across FROM, SUBJECT, and TEXT
        search_query = f'(OR (OR FROM "{query}" SUBJECT "{query}") TEXT "{query}")'
        status, data = imap.search(None, search_query)
        
        if status != "OK" or not data[0]:
            status, data = imap.search(None, f'SUBJECT "{query}"')
            
        if not data[0]:
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
            idx = 0
            for item in fetch_data:
                if isinstance(item, tuple):
                    header_msg = email.message_from_bytes(item[1])
                    mid_str = target_ids[idx].decode("utf-8") if idx < len(target_ids) else ""
                    idx += 1
                    results.append({
                        "id": mid_str,
                        "from": decode_mime_words(header_msg.get("From", "")),
                        "to": decode_mime_words(header_msg.get("To", "")),
                        "subject": decode_mime_words(header_msg.get("Subject", "")),
                        "date": header_msg.get("Date", "")
                    })
                
        imap.logout()
        return json.dumps({"status": "success", "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

# ==============================================================================
# 2. SENDING & ATTACHMENT & THREAD REPLY TOOLS
# ==============================================================================

@mcp.tool()
def send_email(to: str, subject: str, body: str, cc: Optional[str] = None, bcc: Optional[str] = None, html_body: Optional[str] = None) -> str:
    """Sends an email dynamically via SMTP.
    
    Args:
        to: Recipient email address (or comma-separated addresses).
        subject: Dynamic subject line of the email.
        body: Plain text content of the email.
        cc: Optional CC email address.
        bcc: Optional BCC email address.
        html_body: Optional HTML formatted content of the email.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        msg = MIMEMultipart("alternative")
        msg["From"] = sender_email
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
            
        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            recipients.extend([r.strip() for r in cc.split(",") if r.strip()])
        if bcc:
            recipients.extend([r.strip() for r in bcc.split(",") if r.strip()])
            
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"])
        server.login(sender_email, cfg["app_password"])
        server.sendmail(sender_email, recipients, msg.as_string())
        server.quit()
        
        return json.dumps({"status": "success", "message": f"Email successfully sent to {to}"})
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

@mcp.tool()
def send_email_with_attachment(to: str, subject: str, body: str, file_path: str, cc: Optional[str] = None, html_body: Optional[str] = None) -> str:
    """Sends an email with a file attachment dynamically.
    
    Args:
        to: Recipient email address.
        subject: Dynamic subject line.
        body: Plain text email body.
        file_path: Local path to any file to attach.
        cc: Optional CC address.
        html_body: Optional HTML formatted email body.
    """
    try:
        cfg = load_config()
        sender_email = cfg["sender_email"]
        
        if not os.path.exists(file_path):
            return json.dumps({"status": "error", "message": f"Attachment file not found at: {file_path}"})
            
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
            
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            recipients.extend([r.strip() for r in cc.split(",") if r.strip()])
            
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
