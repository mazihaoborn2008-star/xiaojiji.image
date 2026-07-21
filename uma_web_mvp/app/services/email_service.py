"""
Email service for sending verification codes via SMTP.
Supports QQ Mail (465 SSL), Gmail, Outlook, and school email.
"""
import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from ..config import Settings

logger = logging.getLogger(__name__)


def _send_sync(settings: Settings, to_email: str, subject: str, html_body: str) -> bool:
    """Synchronous SMTP send. Called from thread pool."""
    from_address = settings.smtp_from_address
    if not from_address:
        logger.error("SMTP_FROM_EMAIL or SMTP_USERNAME not configured")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header(settings.smtp_from_name, "utf-8")), from_address))
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.smtp_use_ssl and settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_connect_timeout,
            )
            server.ehlo()
        else:
            server = smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_connect_timeout,
            )
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(settings.smtp_username, settings.smtp_password)
        server.sendmail(from_address, [to_email], msg.as_string())
        server.quit()
        logger.info("Email sent to %s", _mask_email(to_email))
        return True
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending to %s: %s", _mask_email(to_email), type(exc).__name__)
        return False
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", _mask_email(to_email), type(exc).__name__)
        return False


def send_verification_email(settings: Settings, to_email: str, code: str) -> bool:
    """
    Send a 6-digit verification code email synchronously.
    Async endpoints must call this through a bounded worker thread.
    Returns True on success, False on failure.
    Never logs the code or SMTP credentials.
    """
    if not settings.is_email_auth_available():
        logger.error("Email auth not available: check EMAIL_AUTH_ENABLED, SMTP_USERNAME, SMTP_PASSWORD")
        return False

    subject = "小击击生图 - 登录验证码"
    html_body = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; color: #333;">
  <div style="max-width: 480px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 32px;">
    <h2 style="margin: 0 0 16px; color: #1a1a1a;">小击击生图 - 登录验证码</h2>
    <p style="margin: 0 0 24px; color: #555;">你正在登录小击击生图网站。</p>
    <div style="background: #f5f5f5; border-radius: 6px; padding: 16px; text-align: center; margin-bottom: 24px;">
      <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #1a1a1a;">{code}</span>
    </div>
    <p style="margin: 0 0 8px; color: #888; font-size: 13px;">验证码 5 分钟内有效，请勿转发给任何人。</p>
    <p style="margin: 0; color: #888; font-size: 13px;">如果不是你本人操作，可以忽略这封邮件。</p>
  </div>
</body>
</html>
"""

    return _send_sync(settings, to_email, subject, html_body)


def _mask_email(email: str) -> str:
    """Mask email for logging: u***@qq.com"""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "***"
    else:
        masked = local[0] + "***" + local[-1]
    return f"{masked}@{domain}"
