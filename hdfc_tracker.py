import imaplib
import email
from email.header import decode_header
from bs4 import BeautifulSoup
import re
import os
import smtplib
from datetime import date, datetime,timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_USER = os.getenv("GMAIL_USER")
EMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT = os.getenv("RECIPIENT_EMAIL")

# HDFC Specific Keywords
HDFC_SENDERS = ["alerts@hdfcbank.bank.in", "netbanking@hdfcbank.com"]
SEARCH_SUBJECT = "You have done a UPI txn"  # Focus only on money going out

def connect_to_mailbox():
    """Connect to Gmail IMAP server"""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")
    return mail

def get_email_body(msg):
    """Extract text content from email (handles HTML and Plain Text)"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            
            # Skip attachments
            if "attachment" in content_disposition:
                continue
            
            if content_type == "text/plain":
                body = part.get_payload(decode=True).decode(errors="ignore")
                break # Prefer plain text if available
            elif content_type == "text/html" and not body:
                body = part.get_payload(decode=True).decode(errors="ignore")
    else:
        body = msg.get_payload(decode=True).decode(errors="ignore")
    
    # If HTML, clean it using BeautifulSoup
    if "<html" in body.lower():
        soup = BeautifulSoup(body, "html.parser")
        body = soup.get_text()
        
    return body

def parse_hdfc_transaction(body, subject):
    """Extract Amount and Merchant from HDFC Email Body"""
    transaction = {}
    
    # 1. Extract Amount (Handles formats like 1,000.00 or 500)
    # Looks for "INR 1,250.00 has been debited" or "Rs. 500 debited"
    amount_pattern = r"(?:Rs\.?|INR)\s*([\d,]+\.?\d*)\s+has\s+been\s+debited"
    amount_match = re.search(amount_pattern, body, re.IGNORECASE)
    
    if amount_match:
        amount_str = amount_match.group(1).replace(",", "")
        transaction['amount'] = float(amount_str)
    else:
        return None # Not a valid debit email

    # 2. Extract Merchant/Description
    # HDFC usually says "at merchant <NAME>" or "towards payment to <NAME>"
    merchant_pattern = r"(?:to VPA\s+\S+\s+|at merchant\s+|towards\s+payment\s+to\s+|favour of\s+)(.+?)\s+(?:on|at|for|\.|\n)"
    merchant_match = re.search(merchant_pattern, body, re.IGNORECASE)
    
    if merchant_match:
        transaction['merchant'] = merchant_match.group(1).strip()
    else:
        transaction['merchant'] = "Unknown Merchant"

    # 3. Extract Date/Time (Optional, usually email date is enough)
    transaction['subject'] = subject
    
    return transaction

def debug_mailbox(mail):
    """Debug function to see what's in your inbox"""
    
    # 1. Check total messages in inbox
    status, data = mail.select("inbox")
    print(f"Connection Status: {status}")
    
    status, messages = mail.search(None, "ALL")
    print(f"Total emails in inbox: {len(messages[0].split()) if messages[0] else 0}")
    
    # 2. Try searching for HDFC emails (without date filter)
    status, messages = mail.search(None, '(FROM "alerts@hdfcbank.bank.in")')
    print(f"HDFC emails found: {len(messages[0].split()) if messages[0] else 0}")
    
    # 3. Try searching for 'Debited' in subject
    status, messages = mail.search(None, '(SUBJECT "Account update")')
    print(f"Emails with 'Account update' in subject: {len(messages[0].split()) if messages[0] else 0}")
    
    # 4. Show last 5 email subjects to verify connection
    status, messages = mail.search(None, "ALL")
    if messages[0]:
        msg_ids = messages[0].split()[-5:]  # Last 5 emails
        for msg_id in msg_ids:
            res, msg = mail.fetch(msg_id, "(RFC822)")
            for response in msg:
                if isinstance(response, tuple):
                    msg_obj = email.message_from_bytes(response[1])
                    subject = msg_obj.get("Subject")
                    date = msg_obj.get("Date")
                    print(f"  - {date} | {subject}")

def fetch_today_transactions(mail):
    """Fetch HDFC emails from today - FIXED VERSION"""
    
    transactions = []
    processed_ids = set()  # Track processed Message-IDs to avoid duplicates
    today = date.today()
    print(f"📅 System Date: {today}")
    
    # Search only today (IMAP format: DD-MMM-YYYY with dashes)
    search_date = today.strftime("%d-%b-%Y")
    tomorrow_date = (today + timedelta(days=1)).strftime("%d-%b-%Y")
    search_query = f'(SINCE "{search_date}" BEFORE "{tomorrow_date}")'
    print(f"🔍 Searching emails SINCE: {search_date} BEFORE: {tomorrow_date}")
    
    status, messages = mail.search(None, search_query)
    
    if status != "OK":
        print(f"❌ IMAP Search Failed: {status}")
        return transactions
    
    if not messages[0]:
        print("ℹ️ No messages found in date range")
        return transactions
    
    msg_ids = messages[0].split()
    print(f"📬 Found {len(msg_ids)} emails in date range")

    for msg_id in msg_ids:
        try:
            res, fetch_data = mail.fetch(msg_id, "(RFC822)")  # ← Renamed to fetch_data
            
            for response_part in fetch_data:  # ← Renamed loop variable
                if isinstance(response_part, tuple):
                    # ← FIXED: Use different variable name for parsed email
                    email_msg = email.message_from_bytes(response_part[1])
                    
                    # Get Message-ID for deduplication
                    message_id = email_msg.get("Message-ID", "")
                    if message_id in processed_ids:
                        print(f"  ⏭️  Skipping duplicate: {message_id}")
                        continue
                    processed_ids.add(message_id)
                    
                    # Decode Subject
                    subject, encoding = decode_header(email_msg["Subject"])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding if encoding else "utf-8")
                    
                    # Check if sender is HDFC
                    sender = email_msg.get("From", "")
                    if not any(hdfc in sender for hdfc in HDFC_SENDERS):
                        continue
                    
                    # Check subject (HDFC uses "Account update" or "You have done a UPI txn")
                    if "Account update" not in subject and "UPI txn" not in subject:
                        continue

                    body = get_email_body(email_msg)
                    
                    # Skip CREDIT transactions (money coming in)
                    if "credited" in body.lower():
                        print(f"  ⏭️  Skipping credit: {subject}")
                        continue
                    
                    txn = parse_hdfc_transaction(body, subject)
                    
                    if txn:
                        transactions.append(txn)
                        print(f"  ✅ DEBIT: {txn['merchant']} - ₹{txn['amount']}")
                    
        except Exception as e:
            print(f"⚠️ Error processing message {msg_id}: {e}")
            continue
    
    print(f"\n📊 Summary: {len(transactions)} unique debits processed")
    return transactions

def send_eod_report(transactions):
    """Send the summary email"""
    total_spend = sum(t['amount'] for t in transactions)
    
    # Construct Email Body
    body = f"<h3>Daily Expense Report (HDFC)</h3>"
    body += f"<p><strong>Date:</strong> {date.today()}</p>"
    body += f"<p><strong>Total Spent:</strong> ₹{total_spend:,.2f}</p>"
    body += "<hr><ul>"
    
    for txn in transactions:
        body += f"<li>{txn['merchant']}: ₹{txn['amount']:,.2f}</li>"
    
    body += "</ul>"
    
    if not transactions:
        body += "<p>No transactions found today.</p>"

    # Send Email
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        
        msg = email.message.EmailMessage()
        msg["Subject"] = f"💸 EOD Expense Report: ₹{total_spend:,.2f}"
        msg["From"] = EMAIL_USER
        msg["To"] = RECIPIENT
        msg.set_content(body, subtype="html")
        
        server.send_message(msg)
    
    print(f"Report sent! Total: ₹{total_spend}")


def main():
    try:
        mail = connect_to_mailbox()
        debug_mailbox(mail)
        transactions = fetch_today_transactions(mail)
        send_eod_report(transactions)
        mail.close()
        mail.logout()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()