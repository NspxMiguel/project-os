"""Pure-logic tests for BirdTunes: safety rules and the recommender.

These cover the two things that must never regress. The safety rules point at a live
animal, so quiet hours and the volume ceiling are enforced in the backend and tested
here. The recommender is the whole point of the like / play-less buttons: if feedback
does not actually move selection probability, the buttons are decoration.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

pytestmark = pytest.mark.usefixtures("home")


# --------------------------------------------------------------------------------- safety


def _schedule_cfg(start: str = "20:00", end: str = "07:00") -> dict:
    return {"enabled": True, "quiet_hours": {"start": start, "end": end}, "windows": []}


@pytest.mark.parametrize(
    "now,expected",
    [
        ("22:30", True),  # after start, before midnight
        ("03:00", True),  # after midnight, before end
        ("06:59", True),  # right up to the boundary
        ("07:00", False),  # end is exclusive: music may start
        ("12:00", False),
        ("19:59", False),
        ("20:00", True),  # start is inclusive
    ],
)
def test_quiet_hours_wrap_around_midnight(now: str, expected: bool) -> None:
    from project_os.apps.birdtunes import safety

    hour, minute = (int(part) for part in now.split(":"))
    moment = dt.datetime(2026, 8, 10, hour, minute)
    assert safety.is_quiet_hours(moment, _schedule_cfg()) is expected


def test_quiet_hours_same_start_and_end_is_never_quiet() -> None:
    """A zero-length range must not be read as 'quiet all day' - that would mean a bird
    room that never gets music and a user with no idea why."""
    from project_os.apps.birdtunes import safety

    cfg = _schedule_cfg("08:00", "08:00")
    for hour in range(24):
        moment = dt.datetime(2026, 8, 10, hour, 30)
        assert safety.is_quiet_hours(moment, cfg) is False


def test_quiet_hours_disabled_schedule_allows_everything() -> None:
    from project_os.apps.birdtunes import safety

    cfg = _schedule_cfg()
    cfg["enabled"] = False
    assert safety.is_quiet_hours(dt.datetime(2026, 8, 10, 23, 0), cfg) is False


@pytest.mark.parametrize(
    "requested,ceiling,expected",
    [(0.9, 0.6, 0.6), (0.5, 0.6, 0.5), (-1.0, 0.6, 0.0), (2.0, 1.0, 1.0), (0.6, 0.6, 0.6)],
)
def test_volume_is_clamped_to_the_configured_ceiling(
    requested: float, ceiling: float, expected: float
) -> None:
    from project_os.apps.birdtunes import safety

    cfg = {"output": {"max_volume": ceiling}}
    assert safety.clamp_volume(requested, cfg) == pytest.approx(expected)


def test_cannot_play_during_quiet_hours_even_with_a_reachable_device() -> None:
    from project_os.apps.birdtunes import safety

    ok, reason = safety.check_can_play(
        dt.datetime(2026, 8, 10, 23, 0), _schedule_cfg(), device_available=True
    )
    assert ok is False
    assert "quiet" in reason.lower()


def test_cannot_play_when_the_speaker_is_unreachable() -> None:
    from project_os.apps.birdtunes import safety

    ok, reason = safety.check_can_play(
        dt.datetime(2026, 8, 10, 12, 0), _schedule_cfg(), device_available=False
    )
    assert ok is False
    assert reason


# ---------------------------------------------------------------------------- recommender


DEFAULT_CFG = {
    "like_boost": 0.8,
    "dislike_decay": 0.35,
    "less_decay": 0.6,
    "recency_window": 8,
    "novelty_bonus": 0.25,
    "explore_rate": 0.0,  # deterministic for tests
}


def _fb(**kwargs) -> dict:
    base = {
        "likes": 0,
        "dislikes": 0,
        "less": 0,
        "plays": 0,
        "skips": 0,
        "completions": 0,
        "blocked": 0,
        "last_played": None,
    }
    base.update(kwargs)
    return base


def _track(track_id: str = "t1", tags=None, **feedback) -> dict:
    return {"id": track_id, "tags": list(tags or []), "feedback": _fb(**feedback)}


NOON = dt.datetime(2026, 8, 10, 12, 0)


def test_liking_a_track_raises_its_score() -> None:
    from project_os.apps.birdtunes import recommender

    neutral = recommender.score_track(_track(plays=3), DEFAULT_CFG, NOON, [])
    liked = recommender.score_track(_track(plays=3, likes=1), DEFAULT_CFG, NOON, [])
    assert liked > neutral


def test_play_less_lowers_the_score_without_silencing_the_track() -> None:
    """'Recommend less' is a nudge, not a ban - the track must still be reachable."""
    from project_os.apps.birdtunes import recommender

    neutral = recommender.score_track(_track(plays=3), DEFAULT_CFG, NOON, [])
    softened = recommender.score_track(_track(plays=3, less=1), DEFAULT_CFG, NOON, [])
    assert 0 < softened < neutral


def test_dislike_hits_harder_than_play_less() -> None:
    from project_os.apps.birdtunes import recommender

    less = recommender.score_track(_track(plays=3, less=1), DEFAULT_CFG, NOON, [])
    disliked = recommender.score_track(_track(plays=3, dislikes=1), DEFAULT_CFG, NOON, [])
    assert disliked < less


def test_blocked_tracks_score_zero() -> None:
    from project_os.apps.birdtunes import recommender

    assert recommender.score_track(_track(blocked=1, likes=5), DEFAULT_CFG, NOON, []) == 0


def test_recently_played_tracks_are_heavily_penalised() -> None:
    from project_os.apps.birdtunes import recommender

    track = _track("t1", plays=3)
    fresh = recommender.score_track(track, DEFAULT_CFG, NOON, ["other", "ids"])
    just_played = recommender.score_track(track, DEFAULT_CFG, NOON, ["t1", "other"])
    assert just_played < fresh


def test_pick_next_is_deterministic_with_an_injected_rng() -> None:
    from project_os.apps.birdtunes import recommender

    tracks = [
        {"id": "a", "tags": [], "feedback": _fb(plays=1)},
        {"id": "b", "tags": [], "feedback": _fb(plays=1)},
        {"id": "c", "tags": [], "feedback": _fb(plays=1)},
    ]
    first = recommender.pick_next(tracks, DEFAULT_CFG, NOON, [], random.Random(1234))
    second = recommender.pick_next(tracks, DEFAULT_CFG, NOON, [], random.Random(1234))
    assert first["id"] == second["id"]


def test_feedback_actually_shifts_selection_over_many_draws() -> None:
    """The end-to-end claim the buttons make: liked tracks show up more, 'less' shows up
    less. Probabilistic, so assert on a large sample with a fixed seed."""
    from project_os.apps.birdtunes import recommender

    tracks = [
        {"id": "loved", "tags": [], "feedback": _fb(plays=5, likes=3)},
        {"id": "plain", "tags": [], "feedback": _fb(plays=5)},
        {"id": "meh", "tags": [], "feedback": _fb(plays=5, less=2)},
    ]
    rng = random.Random(7)
    counts = {"loved": 0, "plain": 0, "meh": 0}
    for _ in range(3000):
        chosen = recommender.pick_next(tracks, DEFAULT_CFG, NOON, [], rng)
        counts[chosen["id"]] += 1

    assert counts["loved"] > counts["plain"] > counts["meh"]


def test_pick_next_returns_none_for_an_empty_candidate_set() -> None:
    from project_os.apps.birdtunes import recommender

    assert recommender.pick_next([], DEFAULT_CFG, NOON, [], random.Random(1)) is None


def test_explain_reports_the_factors_behind_a_choice() -> None:
    from project_os.apps.birdtunes import recommender

    factors = recommender.explain(
        {"id": "a", "tags": ["lullaby"], "feedback": _fb(plays=2, likes=1)},
        DEFAULT_CFG,
        NOON,
        [],
    )
    assert isinstance(factors, dict) and factors


# --------------------------------------------------------------------- schedule validation


def test_normalize_schedule_unwraps_the_get_shape() -> None:
    """GET answers {"schedule": {...}}; sending that back must not nest it again."""
    from project_os.apps.birdtunes import scheduler

    result = scheduler.normalize_schedule({"schedule": {"enabled": False, "windows": []}})
    assert result["enabled"] is False
    assert "schedule" not in result


def test_normalize_schedule_accepts_weekday_names() -> None:
    from project_os.apps.birdtunes import scheduler

    result = scheduler.normalize_schedule(
        {"windows": [{"id": "manha", "days": ["mon", "TUE", "sex", 6], "start": "8:00", "end": "09:30"}]}
    )
    window = result["windows"][0]
    assert window["days"] == [0, 1, 4, 6]
    assert window["start"] == "08:00"


def test_normalize_schedule_rejects_a_typo_instead_of_playing_every_day() -> None:
    from project_os.apps.birdtunes import scheduler

    with pytest.raises(ValueError) as excinfo:
        scheduler.normalize_schedule({"windows": [{"days": ["monday!"], "start": "08:00", "end": "09:00"}]})
    assert "monday!" in str(excinfo.value)


def test_normalize_schedule_names_unknown_fields() -> None:
    from project_os.apps.birdtunes import scheduler

    with pytest.raises(ValueError) as excinfo:
        scheduler.normalize_schedule({"enabled": True, "wndows": []})
    assert "wndows" in str(excinfo.value)


def test_unreadable_days_never_widen_a_window(monkeypatch) -> None:
    """A window nobody can parse must not fire; it used to fire every day."""
    import datetime as dt

    from project_os.apps.birdtunes import scheduler

    cfg = {"enabled": True, "windows": [{"id": "broken", "days": ["someday"], "start": "00:00", "end": "23:59"}]}
    moment = dt.datetime(2026, 8, 10, 12, 0)
    assert scheduler.active_window(moment, cfg) is None


def test_config_replace_removes_keys_the_file_layer_still_has(tmp_path) -> None:
    """set() merges, so a deleted window used to come back. replace() does not."""
    from project_os.config import Config

    path = tmp_path / "config.yaml"
    path.write_text(
        "schedule:\n  enabled: true\n  windows:\n  - id: old\n  leftover: junk\n", encoding="utf-8"
    )
    config = Config(path=path).load()

    config.set("schedule", {"enabled": False, "windows": []})
    assert config.get("schedule").get("leftover") == "junk", "set() is documented to merge"

    config.replace("schedule", {"enabled": False, "windows": []})
    assert config.get("schedule") == {"enabled": False, "windows": []}
    config.save()
    assert "leftover" not in path.read_text(encoding="utf-8")


def test_an_import_that_downloaded_nothing_after_an_error_is_not_done(tmp_path, monkeypatch) -> None:
    """yt-dlp runs with ignoreerrors, so a refused video returns quietly.

    Reporting "done -- nothing new to import" for that reads as "you already had
    it", which is the opposite of what happened.
    """
    from project_os.apps.birdtunes import sources
    from project_os.db import Database

    db = Database(tmp_path / "b.db")
    db.migrate()
    from project_os.apps.birdtunes import library

    library.register_schema(db)
    job = sources.create_job(db, "https://example.invalid/v", kind="video")

    class FakeDownloadError(Exception):
        pass

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if not download:
                return {"id": "abc", "webpage_url": url, "title": "t"}
            raise FakeDownloadError("The page needs to be reloaded.")

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": FakeDownloadError})})
    monkeypatch.setattr(sources, "_yt_dlp", lambda: fake)

    result = sources.run_job(db, job["id"], str(tmp_path / "dl"))
    assert result["state"] == "error"
    assert "reloaded" in result["message"]
    db.close()


def test_importing_a_link_you_already_have_still_fills_the_playlist(tmp_path, monkeypatch):
    """"adicionar musicas direto do youtube em library playlist e etc".

    The second time you paste a link there is nothing to download, and the
    playlist used to come out empty -- which reads as the button not working.
    """
    from project_os.apps.birdtunes import library, sources
    from project_os.db import Database

    db = Database(tmp_path / "b.db")
    db.migrate()
    library.register_schema(db)

    media = tmp_path / "media"
    media.mkdir()
    song = media / "song.mp3"
    song.write_bytes(b"\0" * 32)
    db.execute(
        "INSERT INTO app_birdtunes_tracks (id, path, title, source, source_id, duration) "
        "VALUES ('t1', ?, 'Ja tenho', 'youtube', 'abc12345678', 10)",
        (str(song),),
    )
    playlist = library.create_playlist(db, "Manha")

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"id": "abc12345678", "webpage_url": url, "title": "Ja tenho"}

    monkeypatch.setattr(
        sources, "_yt_dlp",
        lambda: type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})}),
    )

    job = sources.create_job(
        db, "https://youtu.be/abc12345678", kind="video", playlist_id=playlist["id"])
    result = sources.run_job(db, job["id"], str(tmp_path / "dl"))

    assert result["state"] == "done"
    assert "playlist" in result["message"]
    assert library.get_playlist(db, playlist["id"])["track_count"] == 1
    db.close()


def test_a_video_youtube_swallows_reports_its_real_reason(tmp_path, monkeypatch):
    """ignoreerrors turns a refusal into a silent None; the job must still say why."""
    from project_os.apps.birdtunes import library, sources
    from project_os.db import Database

    db = Database(tmp_path / "b.db")
    db.migrate()
    library.register_schema(db)

    class FakeYDL:
        def __init__(self, opts):
            self.logger = opts.get("logger")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            if not download:
                return {"id": "abc12345678", "webpage_url": url, "title": "Privado"}
            self.logger.error("ERROR: [youtube] abc12345678: Private video.")
            return None

    monkeypatch.setattr(
        sources, "_yt_dlp",
        lambda: type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})}),
    )

    job = sources.create_job(db, "https://youtu.be/abc12345678", kind="video")
    result = sources.run_job(db, job["id"], str(tmp_path / "dl"))

    assert result["state"] == "error"
    # The extractor plumbing is stripped; the sentence is not.
    assert result["message"] == "Private video."
    db.close()


def test_a_refused_download_is_retried_as_another_youtube_client(tmp_path, monkeypatch):
    """"The page needs to be reloaded." on the default client, fine on Android.

    Observed on this machine: the same video failed as the web client and
    downloaded as the Android one, minutes apart. Giving up on the first refusal
    would make importing look broken half the time.
    """
    from project_os.apps.birdtunes import sources

    seen = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            self.logger = opts.get("logger")
            client = ((opts.get("extractor_args") or {}).get("youtube") or {}).get("player_client")
            self.client = client[0] if client else ""
            seen.append(self.client)

        def extract_info(self, url, download=False):
            if self.client != "android":
                self.logger.error("ERROR: [youtube] x: The page needs to be reloaded.")
                return None
            return {"id": "abc12345678", "title": "Passaros", "webpage_url": url}

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})

    info, ydl, error = sources.download_entry(fake, {"format": "bestaudio"}, "https://youtu.be/x")
    assert error == ""
    assert info["id"] == "abc12345678"
    assert ydl.client == "android"
    assert seen[0] == "", "the default client is tried first"


def test_the_same_preset_twice_does_not_become_two_windows():
    """From the screenshot: "While I'm out 09:00 - 17:00" listed twice."""
    from project_os.apps.birdtunes import scheduler

    window = {"id": "while_out", "name": "While I'm out", "start": "09:00", "end": "17:00",
              "days": [0, 1, 2, 3, 4]}
    result = scheduler.normalize_schedule({"windows": [window, dict(window)]})

    assert len(result["windows"]) == 1


def test_two_windows_with_the_same_id_but_different_times_both_survive():
    """Deduping must not eat a real second window -- only exact repeats."""
    from project_os.apps.birdtunes import scheduler

    result = scheduler.normalize_schedule({"windows": [
        {"id": "manha", "name": "manha", "start": "08:00", "end": "09:30"},
        {"id": "manha", "name": "manha", "start": "15:00", "end": "16:00"},
    ]})

    assert len(result["windows"]) == 2
    assert result["windows"][0]["id"] != result["windows"][1]["id"]


def test_a_window_can_be_removed_by_sending_the_list_without_it():
    """The trash button posts the remaining windows; nothing must be resurrected."""
    from project_os.apps.birdtunes import scheduler

    kept = {"id": "manha", "name": "manha", "start": "08:00", "end": "09:30"}
    gone = {"id": "while_out", "name": "While I'm out", "start": "09:00", "end": "17:00"}
    full = scheduler.normalize_schedule({"windows": [kept, gone]})
    assert len(full["windows"]) == 2

    trimmed = scheduler.normalize_schedule({"windows": [kept]})
    assert [w["id"] for w in trimmed["windows"]] == ["manha"]


def test_the_clock_follows_the_configured_timezone_not_the_card(home, monkeypatch):
    """The image boots on UTC; quiet hours must still mean the house's evening.

    On the real Pi this was a three-hour error: "quiet hours 20:00" started at
    17:00 in Brazil. For an app whose only job is deciding when to make noise in
    someone's home, that is the whole product being wrong.
    """
    import datetime as dt

    from project_os.apps.birdtunes import app as birdtunes_app

    class Config(object):
        def __init__(self, zone):
            self.zone = zone

        def get(self, path, default=None):
            return self.zone if path == "system.timezone" else default

    # Named zones on both sides: comparing against Config("") would only measure
    # the timezone of whatever machine runs the suite.
    utc = birdtunes_app._now(Config("UTC"))
    sao = birdtunes_app._now(Config("America/Sao_Paulo"))
    assert isinstance(sao, dt.datetime)
    assert sao.tzinfo is None, "callers compare it against naive times"
    # Three hours behind UTC, give or take the second the test took.
    delta = (utc - sao).total_seconds()
    assert 3 * 3600 - 5 < delta < 3 * 3600 + 5

    # An unusable name must not stop the app from deciding anything.
    assert isinstance(birdtunes_app._now(Config("Nowhere/Nothing")), dt.datetime)
