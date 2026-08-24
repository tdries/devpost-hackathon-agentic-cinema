"""customs.fetch: the YouTube way in, without touching YouTube."""
import pytest

from customs.fetch import FetchError, fetch_youtube, youtube_id

VID = "dQw4w9WgXcQ"


# -- url parsing --

@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=4s",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=share_junk",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://m.youtube.com/watch?v={VID}",
])
def test_the_shapes_people_actually_paste_resolve_to_the_id(url):
    assert youtube_id(url) == VID


@pytest.mark.parametrize("url", [
    "https://vimeo.com/12345678",
    "https://example.com/watch?v=" + VID,
    "https://youtu.be.evil.com/" + VID,
    "https://www.youtube.com/watch?v=too_short",
    "https://www.youtube.com/",
    "ftp://youtu.be/" + VID,
    "javascript:alert(1)",
    "",
])
def test_everything_else_is_refused(url):
    assert youtube_id(url) is None


# -- fetching, with a fake downloader --

class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records opts, serves canned metadata,
    and 'downloads' by writing bytes where outtmpl points."""
    info = {"title": "Solstice Launch Spot!", "duration": 56}
    payload = b"x" * 1024
    calls: list[dict] = []

    def __init__(self, opts):
        self.opts = opts
        type(self).calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(type(self).info)

    def download(self, urls):
        target = self.opts["outtmpl"] % {"ext": "mp4"}
        with open(target, "wb") as f:
            f.write(type(self).payload)


@pytest.fixture(autouse=True)
def _fresh_fake():
    _FakeYDL.info = {"title": "Solstice Launch Spot!", "duration": 56}
    _FakeYDL.payload = b"x" * 1024
    _FakeYDL.calls = []


def test_a_good_link_lands_as_an_mp4_named_after_the_title(tmp_path):
    path = fetch_youtube(f"https://youtu.be/{VID}", tmp_path, 120.0,
                         200 * 1024 * 1024, ydl_cls=_FakeYDL)
    assert path.is_file()
    assert path.suffix == ".mp4"
    assert path.stem == "Solstice_Launch_Spot"  # telemetry labels use the stem
    # the download was capped, canonicalised and merged to mp4
    dl_opts = _FakeYDL.calls[-1]
    assert dl_opts["max_filesize"] == 200 * 1024 * 1024
    assert dl_opts["merge_output_format"] == "mp4"


def test_a_video_over_the_duration_cap_is_refused_before_download(tmp_path):
    _FakeYDL.info["duration"] = 300
    with pytest.raises(FetchError, match="300 seconds"):
        fetch_youtube(f"https://youtu.be/{VID}", tmp_path, 120.0,
                      200 * 1024 * 1024, ydl_cls=_FakeYDL)
    # only the metadata probe ran; no download options were ever built
    assert all("outtmpl" not in c for c in _FakeYDL.calls)


def test_a_live_stream_is_refused(tmp_path):
    _FakeYDL.info["is_live"] = True
    with pytest.raises(FetchError, match="live stream"):
        fetch_youtube(f"https://youtu.be/{VID}", tmp_path, 120.0,
                      200 * 1024 * 1024, ydl_cls=_FakeYDL)


def test_an_oversize_download_is_deleted_and_refused(tmp_path):
    _FakeYDL.payload = b"x" * 2048
    with pytest.raises(FetchError, match="over the 0 MB limit"):
        fetch_youtube(f"https://youtu.be/{VID}", tmp_path, 120.0, 1024,
                      ydl_cls=_FakeYDL)
    assert not list(tmp_path.glob("*.mp4"))


def test_a_non_youtube_url_never_reaches_the_downloader(tmp_path):
    with pytest.raises(FetchError, match="Only YouTube links"):
        fetch_youtube("https://evil.example/video.mp4", tmp_path, 120.0,
                      200 * 1024 * 1024, ydl_cls=_FakeYDL)
    assert _FakeYDL.calls == []
