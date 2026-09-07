"""Which of a recording's releases the release-scoped tags come from.

An ISRC identifies a *recording*, not a release, so a MusicBrainz recording
search answers with every compilation the track was ever licensed to. The
pick used to be made on catalogue completeness alone — a barcode, a label,
a country, an "Official" status — which handed the win to whichever
big-label sampler happened to be filled in most thoroughly.

Measured on a real download of Sfera Ebbasta's "Famoso": "UHLALA" was
tagged as track 21 of the 43-track "Hot Party Winter 2021" while TRACKTOTAL
stayed at the source's 17, so the file came out numbered "21/17"; "Baby
(with J Balvin)" landed on "Hot Party Spring 2021" outright. These tests
cover the album-aware pick that replaced it, and the two related mixups it
exposed: barcodes read off a different release than the chosen one, and
multi-disc releases numbered against disc 1 whatever disc the track is on.
"""

from __future__ import annotations

from SpotiFLAC.core import musicbrainz as mb


def _release(
    title,
    *,
    rel_id="rel-1",
    barcode=None,
    label=None,
    country="IT",
    status="Official",
    media=None,
):
    r = {
        "id": rel_id,
        "title": title,
        "country": country,
        "status": status,
        "media": media if media is not None else [{"position": 1, "track-count": 17}],
    }
    if barcode:
        r["barcode"] = barcode
    if label:
        r["label-info"] = [{"label": {"name": label}, "catalog-number": "CAT-1"}]
    return r


# --- the pick itself ------------------------------------------------------


def test_album_beats_a_better_furnished_compilation():
    """The reported bug: a sampler outscoring the album it was asked for."""
    compilation = _release(
        "Hot Party Winter 2021",
        rel_id="comp",
        barcode="1111111111111",
        label="Universal",
        media=[{"position": 1, "track-count": 43}],
    )
    album = _release("Famoso", rel_id="album")

    assert mb._pick_release([compilation, album], "Famoso", 17) is album
    # Without an album to go on, the old ordering is what is left.
    assert mb._pick_release([compilation, album]) is compilation


def test_track_count_separates_editions_of_the_same_album():
    """13-track original vs 17-track reissue — TRACKTOTAL says which."""
    original = _release(
        "Famoso",
        rel_id="orig",
        barcode="2222222222222",
        label="Universal",
        media=[{"position": 1, "track-count": 13}],
    )
    reissue = _release("Famoso", rel_id="reissue")

    assert mb._pick_release([original, reissue], "Famoso", 17) is reissue
    assert mb._pick_release([original, reissue], "Famoso", 13) is original


def test_edition_suffix_still_names_the_same_record():
    deluxe = _release("Famoso (Deluxe Edition)", rel_id="deluxe")
    other = _release("Hot Party Summer 2021", rel_id="other", barcode="3333333333333")
    assert mb._pick_release([deluxe, other], "Famoso", 17) is deluxe


def test_unrelated_album_does_not_match_on_a_shared_word():
    assert not mb._album_matches("Famoso", "Hot Party Winter 2021")
    assert not mb._album_matches("", "Famoso")
    assert mb._album_matches("Famoso", "Famoso (Deluxe)")
    assert mb._album_matches("Privè", "Prive")


# --- release-scoped fields come from the release that was picked ----------


def test_barcode_is_read_off_the_chosen_release():
    """A file used to get one release's ALBUMID and another's UPC."""
    chosen = _release("Famoso", rel_id="chosen", barcode="602435654959", label="UMI")
    stray = _release("Hot Party", rel_id="stray", barcode="0602435502410", label="X")

    parsed = mb._parse_mb_response(
        {"recordings": [{"id": "r1", "releases": [stray, chosen]}]},
        "Famoso",
        17,
    )
    assert parsed["mbid_album"] == "chosen"
    assert parsed["barcode"] == "602435654959"


def test_other_releases_still_fill_in_what_the_chosen_one_lacks():
    chosen = _release("Famoso", rel_id="chosen")
    other = _release("Hot Party", rel_id="other", barcode="999", label="Universal")

    parsed = mb._parse_mb_response(
        {"recordings": [{"id": "r1", "releases": [chosen, other]}]},
        "Famoso",
        17,
    )
    assert parsed["mbid_album"] == "chosen"
    assert parsed["barcode"] == "999"
    assert parsed["label"] == "Universal"


# --- multi-disc numbering -------------------------------------------------


def _two_disc_release():
    return _release(
        "Famoso",
        media=[
            {
                "position": 1,
                "track-count": 10,
                "tracks": [{"position": 1, "number": "1", "recording": {"id": "x"}}],
            },
            {
                "position": 2,
                "track-count": 7,
                "tracks": [
                    {"position": 3, "number": "3", "recording": {"id": "wanted"}}
                ],
            },
        ],
    )


def test_track_on_disc_two_is_numbered_against_disc_two():
    details = mb._parse_mb_details(
        {"id": "wanted", "title": "Tik Tok", "releases": [_two_disc_release()]},
        "Famoso",
        17,
    )
    assert details["disc_number"] == "2"
    assert details["track_number"] == "3"
    assert details["track_total"] == "7"


def test_no_track_number_when_the_recording_is_on_no_medium():
    """Better to leave the source's number alone than overwrite it wrongly."""
    details = mb._parse_mb_details(
        {"id": "absent", "title": "Nowhere", "releases": [_two_disc_release()]},
        "Famoso",
        17,
    )
    assert "track_number" not in details


# --- telling two editions of one album apart ------------------------------


def _famoso_editions():
    """The real shape of ISRC ITUM72001137's two releases."""
    original = _release(
        "Famoso",
        rel_id="original",
        barcode="2222222222222",
        label="Universal",
        media=[{"position": 1, "track-count": 13}],
    )
    original["date"] = "2020-11-20"
    reissue = _release(
        "Famoso", rel_id="reissue", media=[{"position": 1, "track-count": 17}]
    )
    reissue["date"] = "2021-10-14"
    return original, reissue


def test_release_date_separates_editions_when_the_total_is_unknown():
    """Spotify answers a *track* URL with total_tracks=0.

    The track is number 1 on the 13-track original and number 2 on the
    17-track reissue, so picking the wrong one numbered the file "2/13".
    """
    original, reissue = _famoso_editions()

    assert (
        _pick := mb._pick_release(
            [original, reissue], "Famoso", 0, "2021-10-14T00:00:00Z"
        )
    ) is reissue, _pick
    assert mb._pick_release([original, reissue], "Famoso", 0, "2020-11-20") is original


def test_a_year_only_match_still_beats_no_match():
    original, reissue = _famoso_editions()
    assert mb._pick_release([original, reissue], "Famoso", 0, "2021-03-01") is reissue


def test_track_count_outranks_the_date_when_both_are_known():
    """A counted total is the harder fact; a date can be a reissue stamp."""
    original, reissue = _famoso_editions()
    assert mb._pick_release([original, reissue], "Famoso", 13, "2021-10-14") is original


def test_no_date_from_the_source_leaves_the_earlier_ordering_alone():
    original, reissue = _famoso_editions()
    assert mb._pick_release([original, reissue], "Famoso", 17) is reissue
    assert mb._pick_release([original, reissue], "Famoso", 0) is original
