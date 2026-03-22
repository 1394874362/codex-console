from src.services.temp_mail import TempMailService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"no prepared response: {method} {url}")
        return self.responses.pop(0)


def test_dreamhunter_create_email_uses_open_settings_and_api_new_address():
    service = TempMailService({
        "base_url": "https://apimail.example.com",
        "api_mode": "auto",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "domains": ["mail.example.com"],
                "defaultDomains": ["mail.example.com"],
            }
        ),
        FakeResponse(
            payload={
                "address": "tester@mail.example.com",
                "jwt": "jwt-123",
                "password": None,
            }
        ),
    ])
    service.http_client = fake_client

    email_info = service.create_email()

    assert email_info["email"] == "tester@mail.example.com"
    assert email_info["jwt"] == "jwt-123"
    assert email_info["domain"] == "mail.example.com"

    detect_call = fake_client.calls[0]
    assert detect_call["method"] == "GET"
    assert detect_call["url"] == "https://apimail.example.com/open_api/settings"

    create_call = fake_client.calls[1]
    assert create_call["method"] == "POST"
    assert create_call["url"] == "https://apimail.example.com/api/new_address"
    assert create_call["kwargs"]["json"]["domain"] == "mail.example.com"
    assert create_call["kwargs"]["headers"]["x-fingerprint"] == "codex-console"


def test_dreamhunter_get_verification_code_reads_api_mails_with_bearer_token():
    service = TempMailService({
        "base_url": "https://apimail.example.com",
        "api_mode": "dreamhunter",
        "domain": "mail.example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "address": "tester@mail.example.com",
                "jwt": "jwt-123",
            }
        ),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "msg-1",
                        "from": "OpenAI <noreply@openai.com>",
                        "subject": "Your verification code",
                        "text": "Your OpenAI verification code is 654321",
                    }
                ],
                "count": 1,
            }
        ),
    ])
    service.http_client = fake_client

    email_info = service.create_email()
    code = service.get_verification_code(email_info["email"], timeout=1)

    assert code == "654321"

    mails_call = fake_client.calls[1]
    assert mails_call["method"] == "GET"
    assert mails_call["url"] == "https://apimail.example.com/api/mails"
    assert mails_call["kwargs"]["headers"]["Authorization"] == "Bearer jwt-123"


def test_dreamhunter_get_verification_code_skips_old_code_after_otp_sent_at():
    service = TempMailService({
        "base_url": "https://apimail.example.com",
        "api_mode": "dreamhunter",
        "domain": "mail.example.com",
    })
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload={
                "address": "tester@mail.example.com",
                "jwt": "jwt-123",
            }
        ),
        FakeResponse(
            payload={
                "results": [
                    {
                        "id": "old-msg",
                        "from": "OpenAI <noreply@openai.com>",
                        "subject": "Old code",
                        "text": "Your OpenAI verification code is 111111",
                        "createdAt": "2026-03-22T14:15:47Z",
                    },
                    {
                        "id": "new-msg",
                        "from": "OpenAI <noreply@openai.com>",
                        "subject": "New code",
                        "text": "Your OpenAI verification code is 222222",
                        "createdAt": "2026-03-22T14:15:58Z",
                    },
                ],
                "count": 2,
            }
        ),
    ])
    service.http_client = fake_client

    email_info = service.create_email()
    service._used_codes[email_info["email"]] = {"111111"}
    code = service.get_verification_code(
        email_info["email"],
        timeout=1,
        otp_sent_at=1774188957,
    )

    assert code == "222222"
