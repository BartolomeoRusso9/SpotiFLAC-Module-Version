import base64

from SpotiFLAC.providers import tidal


def test_parse_manifest_json():
    # parse_manifest expects a JSON object (starts with '{'), not a list
    b = b'{"urls": ["https://example.com/track.flac"], "mimeType": "audio/flac"}'
    encoded = base64.b64encode(b).decode()
    result = tidal.parse_manifest(encoded)
    assert result.direct_url == "https://example.com/track.flac"
    assert result.mime_type == "audio/flac"


def test_parse_dash_manifest():
    # simple DASH MPD with initialization and one segment
    mpd = """<?xml version="1.0"?>
    <MPD>
      <Period>
        <AdaptationSet>
          <SegmentTemplate initialization="init.mp4" media="$Number$.m4a">
            <SegmentTimeline>
              <S r="0" t="0" d="1000" />
            </SegmentTimeline>
          </SegmentTemplate>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    encoded = base64.b64encode(mpd.encode()).decode()
    res = tidal.parse_manifest(encoded)
    assert res.init_url == "init.mp4"
    assert len(res.media_urls) == 1


def test_clean_title_removes_parentheses_and_accents():
    s = "Song (Remastered) [Deluxe]"
    cleaned = tidal._clean_title(s)
    assert "remastered" not in cleaned
    assert "deluxe" not in cleaned
    assert "song" in cleaned
