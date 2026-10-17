"""Local QQ-mail verification for QBind/QGroupBind.

The bot uses a Feishu mailbox only as the SMTP sender.  The recipient is
always derived from the already submitted QQ number and is never accepted as
an arbitrary user-provided address.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Optional, Protocol

_QQ_RE = re.compile(r"^[1-9]\d{4,10}$")
_SMTP_CONNECTION_ATTEMPTS = 2
_SMTP_RETRY_DELAY_SECONDS = 0.75


def _setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read process env first, then the local NoneBot dotenv files.

    NoneBot exposes its standard settings through the driver config, but
    plugin-specific keys are not guaranteed to be promoted there.  Reading
    the two local dotenv files keeps the documented ``.env`` workflow useful
    without putting the SMTP password in source code.
    """

    value = os.getenv(name)
    if value is not None:
        return value
    try:
        from nonebot import get_driver

        value = getattr(get_driver().config, name.lower(), None)
    except Exception:
        value = None
    if value is not None:
        return str(value)
    try:
        from dotenv import dotenv_values

        roots = [Path.cwd(), Path(__file__).resolve().parents[3]]
        for root in dict.fromkeys(roots):
            for filename in (".env.dev", ".env"):
                path = root / filename
                if not path.is_file():
                    continue
                loaded = dotenv_values(path)
                file_value = loaded.get(name)
                if file_value is not None:
                    return str(file_value)
    except Exception:
        pass
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _setting(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    from_address: str
    timeout_seconds: float
    challenge_ttl_seconds: float
    max_attempts: int
    resend_cooldown_seconds: float
    required: bool
    code_secret: str

    @classmethod
    def from_env(cls) -> "SMTPConfig":
        username = (_setting("QBIND_SMTP_USER", "") or "").strip()
        password = _setting("QBIND_SMTP_PASSWORD", "") or ""
        return cls(
            host=(_setting("QBIND_SMTP_HOST", "smtp.feishu.cn") or "").strip(),
            port=int(_setting("QBIND_SMTP_PORT", "465") or "465"),
            use_ssl=_env_bool("QBIND_SMTP_SSL", True),
            username=username,
            password=password,
            from_address=(
                _setting("QBIND_SMTP_FROM", username) or username
            ).strip(),
            timeout_seconds=max(
                5.0, float(_setting("QBIND_SMTP_TIMEOUT", "15") or "15")
            ),
            challenge_ttl_seconds=max(
                60.0,
                float(_setting("QBIND_EMAIL_CODE_TTL_SECONDS", "600") or "600"),
            ),
            max_attempts=max(
                1, int(_setting("QBIND_EMAIL_MAX_ATTEMPTS", "5") or "5")
            ),
            resend_cooldown_seconds=max(
                0.0,
                float(_setting("QBIND_EMAIL_RESEND_COOLDOWN", "60") or "60"),
            ),
            required=_env_bool("QBIND_EMAIL_REQUIRED", False),
            code_secret=_setting("QBIND_EMAIL_CODE_SECRET", "") or password,
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password and self.from_address)

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("QBIND_SMTP_HOST")
        if not self.username:
            missing.append("QBIND_SMTP_USER")
        if not self.password:
            missing.append("QBIND_SMTP_PASSWORD")
        if not self.from_address:
            missing.append("QBIND_SMTP_FROM")
        return missing


class MailDeliveryError(RuntimeError):
    """Raised when the configured SMTP sender cannot deliver a code."""


class MailConfigurationError(RuntimeError):
    """Raised when email verification is required but SMTP is incomplete."""


class MailSender(Protocol):
    async def send_code(
        self, *, recipient: str, code: str, purpose: str, expires_minutes: int
    ) -> None:
        """Deliver one code to one derived QQ mailbox."""


def qq_email(qq_number: str) -> str:
    value = str(qq_number).strip()
    if not _QQ_RE.fullmatch(value):
        raise ValueError("QQ number cannot be converted to a standard QQ mailbox")
    return f"{value}@qq.com"


def mask_email(address: str) -> str:
    local, separator, domain = address.partition("@")
    if not separator or len(local) < 3:
        return "***@" + (domain or "qq.com")
    return f"{local[:2]}{'*' * max(2, len(local) - 2)}@{domain}"


def email_digest(scope: str, address: str) -> str:
    return hashlib.sha256(
        f"{str(scope)}\x00{address.strip().lower()}".encode("utf-8")
    ).hexdigest()[:32]


def create_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def code_digest(challenge_id: str, code: str, secret: str) -> str:
    key = (secret or "qbind-local-development-key").encode("utf-8")
    message = f"{challenge_id}\x00{code}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _purpose_label(purpose: str) -> str:
    return {
        "personal_bind": "个人 QQ 绑定",
        "group_bind": "群主 QGroupBind 绑定",
        "group_rebind": "群主 QGroupBind 换绑",
    }.get(str(purpose), "身份绑定")


def build_verification_message(
    *,
    from_address: str,
    recipient: str,
    code: str,
    purpose: str,
    expires_minutes: int,
    sent_at: Optional[datetime] = None,
) -> EmailMessage:
    """Build the HTML verification mail and its plain-text fallback."""

    purpose_label = _purpose_label(purpose)
    sent_at_text = (sent_at or datetime.now().astimezone()).strftime(
        "%Y-%m-%d %H:%M %Z"
    )
    plain_text = (
        "Amia 身份认证\n\n"
        "您好，\n\n"
        f"我们收到了一个 {purpose_label} 请求。\n\n"
        f"验证码：{code}\n"
        f"有效期：{expires_minutes} 分钟\n"
        f"发送时间：{sent_at_text}\n\n"
        "如果这是你本人发起的操作，无需其他处理。\n\n"
        "如果不是你本人发起的操作，请忽略此邮件，并检查账号安全。\n\n"
        "此致\n"
        "Amia_晓山瑞希 | HongXing技术团队\n\n"
        "如有疑问，请联系 Amia 管理员。"
    )

    safe_purpose = escape(purpose_label)
    safe_code = escape(str(code))
    safe_expires = escape(f"{expires_minutes} 分钟")
    safe_sent_at = escape(sent_at_text)
    body_style = (
        "margin:0;padding:0;background:#f4f8fa;color:#202124;"
        "font-family:Arial,'Microsoft YaHei','Noto Sans SC',sans-serif;"
    )
    outer_table_style = "border-collapse:collapse;background:#f4f8fa;"
    panel_style = "max-width:680px;border-collapse:collapse;background:#ffffff;"
    logo_style = (
        "font-size:38px;line-height:1.15;font-weight:700;"
        "letter-spacing:-1.5px;color:#111111;"
    )
    content_style = "padding:20px 72px 0;font-size:16px;line-height:1.65;"
    info_table_style = (
        "border-collapse:collapse;width:100%;font-size:16px;"
        "line-height:1.7;"
    )
    label_style = "padding:5px 18px 5px 0;white-space:nowrap;"
    code_style = (
        "padding:5px 0;font-size:30px;line-height:1.25;"
        "font-weight:700;letter-spacing:6px;color:#111111;"
    )
    footer_note_style = (
        "margin:0 0 38px;font-size:13px;line-height:1.6;color:#555555;"
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN">
  <body style="{body_style}">
    <table
      role="presentation"
      width="100%"
      cellspacing="0"
      cellpadding="0"
      border="0"
      style="{outer_table_style}"
    >
      <tr>
        <td style="height:8px;background:#eaf3f7;font-size:0;line-height:0;">&nbsp;</td>
      </tr>
      <tr>
        <td align="center" style="padding:0 16px 32px;">
          <table
            role="presentation"
            width="100%"
            cellspacing="0"
            cellpadding="0"
            border="0"
            style="{panel_style}"
          >
            <tr>
              <td style="padding:46px 72px 18px;">
                <div style="{logo_style}">Amia</div>
              </td>
            </tr>
            <tr>
              <td style="{content_style}">
                <p style="margin:0 0 24px;">您好，</p>
                <p style="margin:0 0 24px;">
                  我们收到了一个 <strong>{safe_purpose}</strong> 请求。
                </p>
                <table
                  role="presentation"
                  cellspacing="0"
                  cellpadding="0"
                  border="0"
                  style="{info_table_style}"
                >
                  <tr>
                    <td style="{label_style}"><strong>验证码</strong></td>
                    <td style="{code_style}">{safe_code}</td>
                  </tr>
                  <tr>
                    <td style="{label_style}"><strong>有效期</strong></td>
                    <td style="padding:5px 0;">{safe_expires}</td>
                  </tr>
                  <tr>
                    <td style="{label_style}"><strong>发送时间</strong></td>
                    <td style="padding:5px 0;">{safe_sent_at}</td>
                  </tr>
                </table>
                <p style="margin:42px 0 24px;">
                  如果这是你本人发起的操作，无需其他处理。
                </p>
                <p style="margin:0 0 52px;">
                  如果不是你本人发起的操作，请忽略此邮件，并检查账号安全。
                </p>
                <p style="margin:0 0 4px;">此致</p>
                <p style="margin:0 0 50px;font-weight:700;">
                  Amia_晓山瑞希 | HongXing技术团队
                </p>
                <p style="{footer_note_style}">
                  如有疑问，请联系 Amia 管理员。
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    message = EmailMessage()
    message["From"] = from_address
    message["To"] = recipient
    message["Subject"] = "Amia 身份绑定验证码"
    message.set_content(plain_text)
    message.add_alternative(html_text, subtype="html")
    return message


class SMTPMailSender:
    """Small stdlib-only SMTP sender so qbind has no extra mail dependency."""

    def __init__(self, config: SMTPConfig):
        self.config = config

    async def send_code(
        self, *, recipient: str, code: str, purpose: str, expires_minutes: int
    ) -> None:
        try:
            await asyncio.to_thread(
                self._send_sync,
                recipient,
                code,
                purpose,
                expires_minutes,
            )
        except MailDeliveryError:
            raise
        except Exception as error:
            raise MailDeliveryError(f"SMTP 发信失败：{error}") from error

    def _connect(self):
        """Connect and complete TLS before any message is sent.

        A transient Feishu SMTP/CDN edge timeout is safe to retry here because
        this method has not authenticated or sent the message yet.  The send
        phase below is deliberately not retried, which avoids duplicate codes
        when a server has already accepted a message but the response was lost.
        """

        for attempt in range(_SMTP_CONNECTION_ATTEMPTS):
            client = None
            try:
                if self.config.use_ssl:
                    return smtplib.SMTP_SSL(
                        self.config.host,
                        self.config.port,
                        timeout=self.config.timeout_seconds,
                    )
                client = smtplib.SMTP(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                )
                client.starttls()
                return client
            except (OSError, smtplib.SMTPException):
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass
                if attempt + 1 >= _SMTP_CONNECTION_ATTEMPTS:
                    raise
                time.sleep(_SMTP_RETRY_DELAY_SECONDS)

        raise RuntimeError("SMTP connection attempts exhausted")

    def _send_sync(
        self, recipient: str, code: str, purpose: str, expires_minutes: int
    ) -> None:
        message = build_verification_message(
            from_address=self.config.from_address,
            recipient=recipient,
            code=code,
            purpose=purpose,
            expires_minutes=expires_minutes,
        )
        try:
            with self._connect() as client:
                client.login(self.config.username, self.config.password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise MailDeliveryError(f"SMTP 发信失败：{error}") from error


class QbindEmailService:
    """Configuration and code primitives used by the command handlers."""

    def __init__(
        self,
        config: Optional[SMTPConfig] = None,
        sender: Optional[MailSender] = None,
    ):
        self.config = config or SMTPConfig.from_env()
        self.sender: MailSender = sender or SMTPMailSender(self.config)

    @property
    def required(self) -> bool:
        return self.config.required

    def ensure_configured(self) -> None:
        if not self.config.configured:
            missing = ", ".join(self.config.missing_fields()) or "SMTP 配置"
            raise MailConfigurationError(f"QQ 邮箱认证尚未配置：{missing}")

    def new_code(self, challenge_id: str) -> tuple[str, str]:
        code = create_code()
        return code, code_digest(challenge_id, code, self.config.code_secret)

    async def send_code(
        self, *, recipient: str, code: str, purpose: str
    ) -> None:
        expires_minutes = max(1, int(round(self.config.challenge_ttl_seconds / 60)))
        await self.sender.send_code(
            recipient=recipient,
            code=code,
            purpose=purpose,
            expires_minutes=expires_minutes,
        )


__all__ = [
    "MailConfigurationError",
    "MailDeliveryError",
    "MailSender",
    "QbindEmailService",
    "SMTPConfig",
    "SMTPMailSender",
    "build_verification_message",
    "code_digest",
    "create_code",
    "email_digest",
    "mask_email",
    "qq_email",
]
