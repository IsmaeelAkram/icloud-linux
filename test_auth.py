import io
import unittest
from unittest.mock import Mock, patch

import auth
from pyicloud.exceptions import PyiCloudAPIResponseException


def api_error(status, payload):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    response.text = "sensitive response"
    return PyiCloudAPIResponseException(
        "Authentication required for Account.",
        status,
        response,
    )


class SmsValidationTests(unittest.TestCase):
    def test_recognizes_accepted_409_response(self):
        error = api_error(409, {"securityCode": {"valid": True, "code": "secret"}})

        self.assertTrue(auth.sms_code_was_accepted(error))

    def test_rejects_invalid_or_different_error_response(self):
        for status, payload in (
            (401, {"securityCode": {"valid": True}}),
            (409, {"securityCode": {"valid": False}}),
            (409, {"securityCode": {"valid": "true"}}),
            (409, {"securityCode": {"valid": 1}}),
            (409, {"securityCode": {}}),
            (409, {"securityCode": None}),
            (409, {}),
            (409, []),
        ):
            with self.subTest(status=status, payload=payload):
                self.assertFalse(auth.sms_code_was_accepted(api_error(status, payload)))

    def test_rejects_missing_response_and_malformed_json(self):
        self.assertFalse(
            auth.sms_code_was_accepted(PyiCloudAPIResponseException("Rejected", 409))
        )
        error = api_error(409, None)
        error.response.json.side_effect = ValueError("Invalid JSON")

        self.assertFalse(auth.sms_code_was_accepted(error))

    def test_accepted_409_continues_to_trust_session(self):
        api = Mock()
        api._request_sms_2fa_code.return_value = True
        api._validate_sms_code.side_effect = api_error(
            409,
            {"securityCode": {"valid": True, "code": "secret"}},
        )
        api.is_trusted_session = False
        api.trust_session.return_value = True

        with patch("builtins.input", return_value="123456"):
            auth.do_sms_forced(api)

        api.trust_session.assert_called_once_with()

    def test_accepted_409_keeps_already_trusted_session(self):
        api = Mock()
        api._request_sms_2fa_code.return_value = True
        api._validate_sms_code.side_effect = api_error(
            409, {"securityCode": {"valid": True}}
        )
        api.is_trusted_session = True

        with patch("builtins.input", return_value="123456"):
            auth.do_sms_forced(api)

        api.trust_session.assert_not_called()

    def test_exits_when_session_trust_fails(self):
        api = Mock()
        api._request_sms_2fa_code.return_value = True
        api.is_trusted_session = False
        api.trust_session.return_value = False
        stderr = io.StringIO()

        with patch("builtins.input", return_value="123456"), patch(
            "sys.stderr", stderr
        ), self.assertRaises(SystemExit) as raised:
            auth.do_sms_forced(api)

        self.assertEqual(raised.exception.code, 1)
        api.trust_session.assert_called_once_with()
        self.assertIn("did not trust the session", stderr.getvalue())

    def test_invalid_response_does_not_print_sensitive_payload(self):
        api = Mock()
        api._request_sms_2fa_code.return_value = True
        api._validate_sms_code.side_effect = api_error(
            409,
            {"securityCode": {"valid": False, "code": "secret"}},
        )
        stderr = io.StringIO()

        with patch("builtins.input", return_value="123456"), patch(
            "sys.stderr", stderr
        ), self.assertRaises(SystemExit) as raised:
            auth.do_sms_forced(api)

        self.assertEqual(raised.exception.code, 1)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertNotIn("sensitive response", stderr.getvalue())
        self.assertEqual(stderr.getvalue().strip(), "Code validation failed.")
        api.trust_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
