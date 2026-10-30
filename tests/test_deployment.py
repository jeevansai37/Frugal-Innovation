import unittest
from unittest.mock import patch

from app import app, resolve_app_base_url


class DeploymentConfigurationTests(unittest.TestCase):
    def test_explicit_base_url_takes_precedence_and_trims_trailing_slash(self):
        self.assertEqual(
            resolve_app_base_url({
                "APP_BASE_URL": "https://campus.example/",
                "VERCEL_URL": "preview-123.vercel.app",
            }),
            "https://campus.example",
        )

    def test_vercel_preview_host_gets_https_scheme(self):
        self.assertEqual(
            resolve_app_base_url({"VERCEL_URL": "frugal-git-preview.vercel.app"}),
            "https://frugal-git-preview.vercel.app",
        )

    def test_local_default_is_preserved_without_vercel_host(self):
        self.assertEqual(resolve_app_base_url({}), "http://localhost:5000")

    def test_qr_gallery_uses_the_resolved_vercel_preview_url(self):
        preview_url = resolve_app_base_url({"VERCEL_URL": "frugal-preview.vercel.app"})
        with patch("app.APP_BASE_URL", preview_url):
            response = app.test_client().get("/qr-codes")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"https://frugal-preview.vercel.app/complaint?location=M+Block",
            response.data,
        )


if __name__ == "__main__":
    unittest.main()
