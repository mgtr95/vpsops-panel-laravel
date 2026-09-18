import unittest

from app.services.log_match import log_line_is_error


VITE_ASSET = (
    "  2026-09-08 00:50:42 /build/assets/error-BUYI9hKa.js .............. ~ 0.02ms"
)
NGINX_INPUT_ERROR_200 = (
    '192.0.2.1 - - [08/Sep/2026:07:08:40 +0000] '
    '"GET /build/assets/input-error-DEkipNxA.js HTTP/1.1" 200 438 "-" '
    '"Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15" "-"'
)
NGINX_INPUTERROR_200 = (
    '192.0.2.1 - - [08/Sep/2026:03:25:59 +0000] '
    '"GET /build/assets/InputError-Bvo9tIGw.js HTTP/1.1" 200 171 "-" '
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" "-"'
)
CADDY_LOGIN_INFO = (
    "2026/09/08 07:34:34.310\tINFO\thttp.log.access.log0\thandled request\t"
    '{"request": {"remote_ip": "192.0.2.10", "method": "GET", '
    '"host": "app.example.com", "uri": "/login"}, '
    '"status": 200, "size": 1585, '
    '"resp_headers": {"Link": '
    '["<https://app.example.com/build/assets/input-error-CSSQMlrA.js>; '
    'rel=\\"modulepreload\\"; as=\\"script\\""]}}'
)


class LogMatchTests(unittest.TestCase):
    def test_vite_error_asset_is_ignored(self):
        self.assertFalse(log_line_is_error(VITE_ASSET))

    def test_nginx_input_error_js_200_is_ignored(self):
        self.assertFalse(log_line_is_error(NGINX_INPUT_ERROR_200))

    def test_nginx_inputerror_component_200_is_ignored(self):
        self.assertFalse(log_line_is_error(NGINX_INPUTERROR_200))

    def test_caddy_info_login_with_input_error_preload_is_ignored(self):
        self.assertFalse(log_line_is_error(CADDY_LOGIN_INFO))

    def test_get_error_path_200_is_ignored(self):
        line = (
            '1.2.3.4 - - [08/Sep/2026:07:08:40 +0000] '
            '"GET /error HTTP/1.1" 200 12 "-" "Mozilla/5.0" "-"'
        )
        self.assertFalse(log_line_is_error(line))

    def test_env_traceback_probe_301_is_ignored(self):
        line = (
            '192.0.2.1 - - [14/Sep/2026:06:42:49 +0000] '
            '"GET /.env-traceback HTTP/1.1" 301 169 "-" '
            '"Opera/7.03 (Windows NT 5.1; U)  [en]" "-"'
        )
        self.assertFalse(log_line_is_error(line))

    def test_http_404_is_ignored(self):
        line = (
            '1.2.3.4 - - [08/Sep/2026:07:08:40 +0000] '
            '"GET /wp-login.php HTTP/1.1" 404 12 "-" "-" "-"'
        )
        self.assertFalse(log_line_is_error(line))

    def test_http_500_is_matched(self):
        line = (
            '1.2.3.4 - - [08/Sep/2026:07:08:40 +0000] '
            '"GET /login HTTP/1.1" 500 99 "-" "-" "-"'
        )
        self.assertTrue(log_line_is_error(line))

    def test_json_status_502_is_matched(self):
        self.assertTrue(log_line_is_error('INFO handled request {"status": 502, "uri": "/login"}'))

    def test_laravel_error_level_is_matched(self):
        self.assertTrue(
            log_line_is_error("[2026-09-08 00:50:42] production.ERROR: Undefined variable $foo")
        )

    def test_nginx_error_level_is_matched(self):
        self.assertTrue(
            log_line_is_error(
                "2026/09/08 07:08:40 [error] 12#12: *1 connect() failed "
                "(111: Connection refused) while connecting to upstream"
            )
        )

    def test_php_fatal_is_matched(self):
        self.assertTrue(log_line_is_error("PHP Fatal error: Uncaught ErrorException: fail in /app/index.php"))

    def test_python_traceback_is_matched(self):
        self.assertTrue(log_line_is_error("Traceback (most recent call last):"))
        self.assertTrue(log_line_is_error("ValueError: invalid literal"))

    def test_caddy_error_level_is_matched(self):
        self.assertTrue(
            log_line_is_error(
                "2026/09/08 07:34:34.310\tERROR\thttp.log.error\t"
                '{"status": 502, "msg": "dial tcp: connection refused"}'
            )
        )

    def test_empty_line_is_ignored(self):
        self.assertFalse(log_line_is_error(""))
        self.assertFalse(log_line_is_error("   "))

    def test_laravel_queue_max_time_shutdown_is_ignored(self):
        self.assertFalse(
            log_line_is_error(
                "The worker has been stopped due to exceeding the configured max time."
            )
        )
        self.assertFalse(
            log_line_is_error(
                "The --max-time option was reached. The worker will exit after completing the current job."
            )
        )


if __name__ == "__main__":
    unittest.main()
