def test_download_reuses_archive_and_honors_etag(app_ctx, monkeypatch):
    from silver_cli import bundle

    bundle._cached_build.cache_clear()
    builds = []

    def build():
        builds.append(True)
        return b"zipapp-content"

    monkeypatch.setattr(bundle, "build", build)
    client = app_ctx.test_client()
    first = client.get("/api/v1/cli/silver-cli.pyz")
    assert first.status_code == 200
    assert first.data == b"zipapp-content"
    second = client.get("/api/v1/cli/silver-cli.pyz",
                        headers={"If-None-Match": first.headers["ETag"]})
    assert second.status_code == 304
    assert second.data == b""
    assert builds == [True]
    assert "must-revalidate" in first.headers["Cache-Control"]
    bundle._cached_build.cache_clear()
