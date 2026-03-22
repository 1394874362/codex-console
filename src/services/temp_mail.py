"""
Temp-Mail 邮箱服务实现。

兼容两类常见的 Cloudflare 临时邮箱接口：

1. 传统 admin 模式
   - POST /admin/new_address
   - GET  /admin/mails
   - Header: x-admin-auth

2. DreamHunter cloudflare_temp_email v1.x
   - GET  /open_api/settings
   - POST /api/new_address
   - GET  /api/mails
   - Header: Authorization: Bearer <jwt>

当前实现会在 `api_mode=auto` 时自动探测：
若 /open_api/settings 可用，则走 DreamHunter 模式；否则回退到 admin 模式。
"""

import json
import logging
import random
import re
import string
import time
from email import message_from_string
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as email_policy
from html import unescape
from typing import Any, Dict, List, Optional

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import OTP_CODE_PATTERN
from ..core.http_client import HTTPClient, RequestConfig


logger = logging.getLogger(__name__)


class TempMailService(BaseEmailService):
    """自部署 Cloudflare 临时邮箱服务。"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.TEMP_MAIL, name)

        cfg = config or {}
        if not cfg.get("base_url"):
            raise ValueError("缺少必需配置: ['base_url']")

        default_config = {
            "enable_prefix": True,
            "timeout": 30,
            "max_retries": 3,
            "api_mode": "auto",  # auto / admin / dreamhunter
            "lang": "zh",
            "fingerprint": "codex-console",
        }
        self.config = {**default_config, **cfg}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")

        self.http_client = HTTPClient(
            proxy_url=None,
            config=RequestConfig(
                timeout=self.config["timeout"],
                max_retries=self.config["max_retries"],
            ),
        )

        # email -> info(jwt, address, ...)
        self._email_cache: Dict[str, Dict[str, Any]] = {}
        self._api_mode_cache: Optional[str] = None
        self._open_settings_cache: Optional[Dict[str, Any]] = None

    # ---------------------------
    # common helpers
    # ---------------------------

    def _decode_mime_header(self, value: str) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _extract_body_from_message(self, message: Message) -> str:
        parts: List[str] = []

        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue

                content_type = (part.get_content_type() or "").lower()
                if content_type not in ("text/plain", "text/html"):
                    continue

                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    try:
                        text = part.get_content()
                    except Exception:
                        text = ""

                if content_type == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
        else:
            try:
                payload = message.get_payload(decode=True)
                charset = message.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                try:
                    body = message.get_content()
                except Exception:
                    body = str(message.get_payload() or "")

            if "html" in (message.get_content_type() or "").lower():
                body = re.sub(r"<[^>]+>", " ", body)
            parts.append(body)

        return unescape("\n".join(part for part in parts if part).strip())

    def _extract_mail_fields(self, mail: Dict[str, Any]) -> Dict[str, str]:
        sender = str(
            mail.get("source")
            or mail.get("from")
            or mail.get("from_address")
            or mail.get("fromAddress")
            or ""
        ).strip()
        subject = str(mail.get("subject") or mail.get("title") or "").strip()
        body_text = str(
            mail.get("text")
            or mail.get("body")
            or mail.get("content")
            or mail.get("html")
            or ""
        ).strip()
        raw = str(mail.get("raw") or "").strip()

        if raw:
            try:
                message = message_from_string(raw, policy=email_policy)
                sender = sender or self._decode_mime_header(message.get("From", ""))
                subject = subject or self._decode_mime_header(message.get("Subject", ""))
                parsed_body = self._extract_body_from_message(message)
                if parsed_body:
                    body_text = f"{body_text}\n{parsed_body}".strip() if body_text else parsed_body
            except Exception as e:
                logger.debug(f"解析 TempMail raw 邮件失败: {e}")
                body_text = f"{body_text}\n{raw}".strip() if body_text else raw

        body_text = unescape(re.sub(r"<[^>]+>", " ", body_text))
        return {
            "sender": sender,
            "subject": subject,
            "body": body_text,
            "raw": raw,
        }

    def _admin_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        admin_password = str(self.config.get("admin_password") or "").strip()
        if admin_password:
            headers["x-admin-auth"] = admin_password
        return headers

    def _dreamhunter_headers(self, jwt: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-lang": self.config.get("lang", "zh"),
            "x-fingerprint": self.config.get("fingerprint", "codex-console"),
        }

        custom_auth = str(
            self.config.get("x_custom_auth")
            or self.config.get("custom_auth")
            or self.config.get("auth")
            or ""
        ).strip()
        if custom_auth:
            headers["x-custom-auth"] = custom_auth

        admin_password = str(self.config.get("admin_password") or "").strip()
        if admin_password:
            headers["x-admin-auth"] = admin_password

        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"

        return headers

    def _make_raw_request(
        self,
        method: str,
        path: str,
        *,
        default_headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        for k, v in (default_headers or {}).items():
            kwargs["headers"].setdefault(k, v)
        return self.http_client.request(method, url, **kwargs)

    def _make_json_request(
        self,
        method: str,
        path: str,
        *,
        default_headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> Any:
        try:
            response = self._make_raw_request(
                method,
                path,
                default_headers=default_headers,
                **kwargs,
            )

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        return self._make_json_request(
            method,
            path,
            default_headers=self._admin_headers(),
            **kwargs,
        )

    # ---------------------------
    # mode detect / settings
    # ---------------------------

    def _get_api_mode(self) -> str:
        if self._api_mode_cache:
            return self._api_mode_cache

        configured = str(self.config.get("api_mode") or "auto").strip().lower()
        if configured in {"admin", "dreamhunter"}:
            self._api_mode_cache = configured
            return configured

        try:
            response = self._make_raw_request(
                "GET",
                "/open_api/settings",
                default_headers=self._dreamhunter_headers(),
            )
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict) and isinstance(data.get("domains"), list):
                    self._open_settings_cache = data
                    self._api_mode_cache = "dreamhunter"
                    return self._api_mode_cache
        except Exception as e:
            logger.debug(f"自动探测 DreamHunter API 失败: {e}")

        self._api_mode_cache = "admin"
        return self._api_mode_cache

    def _get_open_settings(self) -> Dict[str, Any]:
        if self._open_settings_cache:
            return self._open_settings_cache

        data = self._make_json_request(
            "GET",
            "/open_api/settings",
            default_headers=self._dreamhunter_headers(),
        )
        if not isinstance(data, dict):
            raise EmailServiceError(f"open_api/settings 返回数据格式错误: {data}")
        self._open_settings_cache = data
        return data

    def _resolve_domain(self, config: Optional[Dict[str, Any]] = None) -> str:
        override = str((config or {}).get("domain") or "").strip()
        if override:
            return override

        domain = str(self.config.get("domain") or "").strip()
        if domain:
            return domain

        if self._get_api_mode() != "dreamhunter":
            raise ValueError("缺少必需配置: ['domain']")

        settings = self._get_open_settings()
        domains = settings.get("defaultDomains") or settings.get("domains") or []
        if not domains:
            raise EmailServiceError("DreamHunter open_api/settings 未返回可用域名")
        return str(domains[0]).strip()

    # ---------------------------
    # service api
    # ---------------------------

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        letters = "".join(random.choices(string.ascii_lowercase, k=5))
        digits = "".join(random.choices(string.digits, k=random.randint(1, 3)))
        suffix = "".join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
        name = letters + digits + suffix

        domain = self._resolve_domain(config)
        enable_prefix = self.config.get("enable_prefix", True)

        try:
            if self._get_api_mode() == "dreamhunter":
                response = self._make_json_request(
                    "POST",
                    "/api/new_address",
                    json={
                        "name": name,
                        "domain": domain,
                        "cf_token": "",
                    },
                    default_headers=self._dreamhunter_headers(),
                )
            else:
                response = self._make_request(
                    "POST",
                    "/admin/new_address",
                    json={
                        "enablePrefix": enable_prefix,
                        "name": name,
                        "domain": domain,
                    },
                )

            address = str(response.get("address") or response.get("email") or "").strip()
            jwt = str(response.get("jwt") or response.get("token") or "").strip()

            if not address:
                raise EmailServiceError(f"API 返回数据不完整: {response}")

            email_info = {
                "email": address,
                "jwt": jwt,
                "password": response.get("password"),
                "service_id": address,
                "id": address,
                "created_at": time.time(),
                "domain": domain,
            }

            self._email_cache[address] = email_info
            logger.info(f"成功创建 TempMail 邮箱: {address}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        logger.info(f"正在从 TempMail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()
        cached = self._email_cache.get(email, {})
        jwt = cached.get("jwt")
        api_mode = self._get_api_mode()

        while time.time() - start_time < timeout:
            try:
                if api_mode == "dreamhunter":
                    if not jwt:
                        logger.warning(f"DreamHunter TempMail 缓存中缺少 JWT，无法查询 {email}")
                        return None
                    response = self._make_json_request(
                        "GET",
                        "/api/mails",
                        params={"limit": 20, "offset": 0},
                        default_headers=self._dreamhunter_headers(jwt=jwt),
                    )
                elif jwt:
                    response = self._make_request(
                        "GET",
                        "/user_api/mails",
                        params={"limit": 20, "offset": 0},
                        headers={
                            "x-user-token": jwt,
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                    )
                else:
                    response = self._make_request(
                        "GET",
                        "/admin/mails",
                        params={"limit": 20, "offset": 0, "address": email},
                    )

                mails = response.get("results", [])
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                for mail in mails:
                    mail_id = mail.get("id")
                    if not mail_id or mail_id in seen_mail_ids:
                        continue

                    seen_mail_ids.add(mail_id)

                    parsed = self._extract_mail_fields(mail)
                    sender = parsed["sender"].lower()
                    subject = parsed["subject"]
                    body_text = parsed["body"]
                    raw_text = parsed["raw"]
                    content = f"{sender}\n{subject}\n{body_text}\n{raw_text}".strip()

                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    match = re.search(pattern, content)
                    if match:
                        code = match.group(1)
                        logger.info(f"从 TempMail 邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"检查 TempMail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 TempMail 验证码超时: {email}")
        return None

    def list_emails(self, limit: int = 100, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        if self._get_api_mode() == "dreamhunter":
            self.update_status(True)
            return list(self._email_cache.values())

        params = {
            "limit": limit,
            "offset": offset,
        }
        params.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            response = self._make_request("GET", "/admin/mails", params=params)
            mails = response.get("results", [])
            if not isinstance(mails, list):
                raise EmailServiceError(f"API 返回数据格式错误: {response}")

            emails: List[Dict[str, Any]] = []
            for mail in mails:
                address = (mail.get("address") or "").strip()
                mail_id = mail.get("id") or address
                email_info = {
                    "id": mail_id,
                    "service_id": mail_id,
                    "email": address,
                    "subject": mail.get("subject"),
                    "from": mail.get("source"),
                    "created_at": mail.get("createdAt") or mail.get("created_at"),
                    "raw_data": mail,
                }
                emails.append(email_info)

                if address:
                    cached = self._email_cache.get(address, {})
                    self._email_cache[address] = {**cached, **email_info}

            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 TempMail 邮箱失败: {e}")
            self.update_status(False, e)
            return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        removed = False
        emails_to_delete = []

        for address, info in self._email_cache.items():
            candidate_ids = {
                address,
                info.get("id"),
                info.get("service_id"),
            }
            if email_id in candidate_ids:
                emails_to_delete.append(address)

        for address in emails_to_delete:
            self._email_cache.pop(address, None)
            removed = True

        if removed:
            logger.info(f"已从 TempMail 缓存移除邮箱: {email_id}")
            self.update_status(True)
        else:
            logger.info(f"TempMail 缓存中未找到邮箱: {email_id}")

        return removed

    def check_health(self) -> bool:
        try:
            if self._get_api_mode() == "dreamhunter":
                self._get_open_settings()
            else:
                self._make_request(
                    "GET",
                    "/admin/mails",
                    params={"limit": 1, "offset": 0},
                )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"TempMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
