"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "response_store_path": "responses.db",
    "response_store_ttl_sec": 86400,
    "response_store_max_rows": 1000,
    "max_tool_output_chars": 12000,
    "max_history_messages": 40,
    "max_history_chars": 60000,
    "max_google_prompt_chars": 18000,
    "google_stream_auto_tools": False,
    "continuation_attempts": 2,
    "sse_heartbeat_sec": 10,
    "reuse_upstream_sessions": False,
    "upstream_session_backend": "gemini_webapi",
    "upstream_session_fallback_direct": True,
    "cookie_cache_path": "/app/data/gemini_cookies",
    "cookie_auto_refresh": True,
    "cookie_refresh_interval_sec": 600,
    "webapi_watchdog_sec": 120,
    "webapi_request_timeout_sec": 180,
    "tool_retry_attempts": 1,
    "temporary_background_tasks": True,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
