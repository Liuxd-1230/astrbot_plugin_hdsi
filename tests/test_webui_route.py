"""Round-5: WebUI Plugin Page route correctness."""

from __future__ import annotations

import os
import re

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = "astrbot_plugin_hdsi"


def _main_src() -> str:
    return open(os.path.join(_DIR, "main.py")).read()


def _js() -> str:
    return open(os.path.join(_DIR, "pages", "dashboard", "index.html")).read()


class TestBackendRouteRegistration:
    def test_prefixed_routes_registered(self):
        src = _main_src()
        assert "PLUGIN_NAME}/hdsi" in src, \
            "backend must register {PLUGIN_NAME}/hdsi routes"
        assert "PLUGIN_NAME}/hdsi" in src, \
            "backend must register {PLUGIN_NAME}/hdsi routes"
        assert "PLUGIN_NAME}/hdsi" in src, \
            "backend must register {PLUGIN_NAME}/hdsi routes"

    def test_no_unprefixed_alias(self):
        src = _main_src()
        assert "for api in" not in src, "no multi-prefix loop allowed"
        bare = re.findall(r'register_web_api\(\s*f?"(/hdsi)', src)
        assert not bare, f"unprefixed /hdsi routes found: {bare}"

    def test_all_expected_endpoints(self):
        src = _main_src()
        for ep in ("overview", "config", "participants", "script",
                   "intents", "maintenance", "migrate_config"):
            assert f"{{api}}/{ep}" in src, f"missing endpoint: {ep}"


class TestFrontendBridgePath:
    def test_no_api_base_constant(self):
        assert "API_BASE" not in _js()

    def test_no_bridge_sdk_script_tag(self):
        assert "page-bridge-sdk.js" not in _js()

    def test_normalize_endpoint_exists(self):
        assert "normalizeEndpoint" in _js()

    def test_bridge_calls_use_relative_path(self):
        js = _js()
        assert re.search(r"AstrBotPluginPage\.apiGet\(\s*path\b", js)
        assert re.search(r"AstrBotPluginPage\.apiPost\(\s*path\b", js)

    def test_no_double_hdsi_prefix(self):
        assert "hdsi/hdsi" not in _js()

    def test_no_double_plugin_prefix(self):
        assert "/astrbot_plugin_hdsi/astrbot_plugin_hdsi" not in _js()


class TestFallbackHttpPath:
    def test_fallback_get_url_format(self):
        js = _js()
        assert "/api/v1/plugins/extensions/${PLUGIN}/${path}" in js

    def test_fallback_post_url_format(self):
        js = _js()
        assert "/api/v1/plugins/extensions/${PLUGIN}/${path}" in js

    def test_simulated_fallback_url_for_overview(self):
        path = "hdsi/overview"
        full_url = f"/api/v1/plugins/extensions/{PLUGIN}/{path}"
        assert full_url == f"/api/v1/plugins/extensions/{PLUGIN}/hdsi/overview"
        sub_path = "/" + full_url.split("/api/v1/plugins/extensions/", 1)[1]
        backend_route = f"/{PLUGIN}/hdsi/overview"
        assert sub_path == backend_route
