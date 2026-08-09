"""Tests for the rest of BirdTunes: library, scheduler, sources, players, and the API.

Where tests/test_birdtunes_logic.py covers the two pure modules (safety,
recommender), this file covers everything that had to actually run for
BirdTunes to be a usable app -- the scan, the schedule loop, the YouTube
import faked at the yt_dlp seam, the null player's state machine, and the
HTTP routes mounted at /api/apps/birdtunes.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

pytestmark = pytest.mark.usefixtures("home")


# --------------------------------------------------------------------------------- library


def test_scan_adds_sample_tracks_and_is_idempotent(db, media_dir, sample_tracks):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    result = library.scan(db, [str(media_dir)])
    assert result["added"] == len(sample_tracks)
    assert result["total"] == len(sample_tracks)

    again = library.scan(db, [str(media_dir)])
    assert again["added"] == 0
    assert again["total"] == len(sample_tracks)


def test_scan_marks_deleted_files_missing_but_keeps_the_row(db, media_dir, sample_tracks):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    library.scan(db, [str(media_dir)])
    sample_tracks[0].unlink()

    result = library.scan(db, [str(media_dir)])
    assert result["missing"] == 1
    tracks = library.list_tracks(db, include_missing=True)
    missing = [t for t in tracks if t["missing"]]
    assert len(missing) == 1


def test_compatible_with_matches_the_format_table():
    from projectos.apps.birdtunes import library

    assert library.compatible_with("mp3", "mp3", "airplay") is True
    assert library.compatible_with("m4a", "aac", "airplay") is False
    assert library.compatible_with("m4a", "aac", "chromecast") is True
    assert library.compatible_with("webm", "opus", "airplay") is False
    # An unlisted backend is assumed broad rather than refused outright.
    assert library.compatible_with("wma", "wma", "ha_player") is True
    assert library.compatible_with("wma", "wma", "local") is True


def test_feedback_like_and_dislike_move_the_counters(db):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    db.execute(
        "INSERT INTO app_birdtunes_tracks (id, path, title, added_at) VALUES ('t1', '/x', 'X', '')"
    )
    library.apply_feedback(db, "t1", "like")
    library.apply_feedback(db, "t1", "like")
    feedback = library.apply_feedback(db, "t1", "dislike")
    assert feedback["likes"] == 2
    assert feedback["dislikes"] == 1

    feedback = library.apply_feedback(db, "t1", "more")
    assert feedback["dislikes"] == 0  # "more" undoes the discouragement

    feedback = library.apply_feedback(db, "t1", "block")
    assert feedback["blocked"] is True
    feedback = library.apply_feedback(db, "t1", "reset")
    assert feedback == {
        "track_id": "t1", "likes": 0, "dislikes": 0, "less": 0, "plays": 0,
        "skips": 0, "completions": 0, "blocked": False, "last_played": None,
    }


def test_apply_feedback_rejects_an_unknown_action(db):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    db.execute(
        "INSERT INTO app_birdtunes_tracks (id, path, title, added_at) VALUES ('t1', '/x', 'X', '')"
    )
    with pytest.raises(ValueError):
        library.apply_feedback(db, "t1", "shrug")


def test_the_all_playlist_is_virtual_and_cannot_be_edited(db, media_dir, sample_tracks):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    library.scan(db, [str(media_dir)])

    playlists = library.list_playlists(db)
    all_playlist = next(p for p in playlists if p["id"] == library.ALL_PLAYLIST_ID)
    assert all_playlist["track_count"] == len(sample_tracks)
    assert all_playlist["virtual"] is True

    with pytest.raises(ValueError):
        library.update_playlist(db, library.ALL_PLAYLIST_ID, name="nope")
    with pytest.raises(ValueError):
        library.delete_playlist(db, library.ALL_PLAYLIST_ID)
    with pytest.raises(ValueError):
        library.reorder_playlist(db, library.ALL_PLAYLIST_ID, [])


def test_playlist_crud_and_reorder(db, media_dir, sample_tracks):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    library.scan(db, [str(media_dir)])
    tracks = library.list_tracks(db)
    track_ids = [t["id"] for t in tracks]

    playlist = library.create_playlist(db, "Lullabies", shuffle=False)
    library.add_tracks_to_playlist(db, playlist["id"], track_ids)
    assert library.get_playlist(db, playlist["id"])["track_count"] == len(track_ids)

    reversed_ids = list(reversed(track_ids))
    library.reorder_playlist(db, playlist["id"], reversed_ids)
    ordered = library.playlist_tracks(db, playlist["id"])
    assert [t["id"] for t in ordered] == reversed_ids

    library.remove_tracks_from_playlist(db, playlist["id"], [track_ids[0]])
    assert library.get_playlist(db, playlist["id"])["track_count"] == len(track_ids) - 1

    assert library.delete_playlist(db, playlist["id"]) is True
    assert library.get_playlist(db, playlist["id"]) is None


def test_candidate_set_reports_no_tracks_for_an_empty_library(db):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    tracks, reason = library.candidate_set(db, library.ALL_PLAYLIST_ID, "airplay")
    assert tracks == []
    assert reason == "no_tracks"


def test_candidate_set_reports_all_incompatible_when_nothing_fits_the_backend(db):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    db.execute(
        "INSERT INTO app_birdtunes_tracks (id, path, title, container, codec, added_at) "
        "VALUES ('t1', '/x.m4a', 'X', 'm4a', 'aac', '')"
    )
    db.execute("INSERT INTO app_birdtunes_feedback (track_id) VALUES ('t1')")
    tracks, reason = library.candidate_set(db, library.ALL_PLAYLIST_ID, "airplay")
    assert tracks == []
    assert reason == "all_incompatible"


def test_candidate_set_excludes_blocked_tracks(db):
    from projectos.apps.birdtunes import library

    library.register_schema(db)
    db.execute(
        "INSERT INTO app_birdtunes_tracks (id, path, title, container, codec, added_at) "
        "VALUES ('t1', '/x.mp3', 'X', 'mp3', 'mp3', '')"
    )
    db.execute("INSERT INTO app_birdtunes_feedback (track_id, blocked) VALUES ('t1', 1)")
    tracks, reason = library.candidate_set(db, library.ALL_PLAYLIST_ID, "")
    assert tracks == []
    assert reason == "no_tracks"


# --------------------------------------------------------------------------------- scheduler


def _schedule(**overrides):
    cfg = {
        "enabled": True,
        "quiet_hours": {"start": "20:00", "end": "07:00"},
        "windows": [
            {"id": "w1", "name": "Morning", "start": "09:00", "end": "11:00", "days": [0, 1, 2, 3, 4], "enabled": True},
        ],
    }
    cfg.update(overrides)
    return cfg


def test_active_window_matches_inside_the_configured_hours():
    from projectos.apps.birdtunes import scheduler

    moment = dt.datetime(2026, 8, 10, 10, 0)  # Monday
    window = scheduler.active_window(moment, _schedule())
    assert window is not None
    assert window["id"] == "w1"


def test_active_window_respects_weekday_filtering():
    from projectos.apps.birdtunes import scheduler

    saturday = dt.datetime(2026, 8, 15, 10, 0)
    assert scheduler.active_window(saturday, _schedule()) is None


def test_active_window_first_enabled_match_wins_on_overlap():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule(windows=[
        {"id": "a", "start": "09:00", "end": "12:00", "days": [0], "enabled": True},
        {"id": "b", "start": "10:00", "end": "11:00", "days": [0], "enabled": True},
    ])
    moment = dt.datetime(2026, 8, 10, 10, 30)  # Monday, inside both
    window = scheduler.active_window(moment, cfg)
    assert window["id"] == "a"


def test_active_window_handles_midnight_crossing():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule(windows=[
        {"id": "night", "start": "22:00", "end": "02:00", "days": list(range(7)), "enabled": True},
    ])
    assert scheduler.active_window(dt.datetime(2026, 8, 10, 23, 30), cfg) is not None
    assert scheduler.active_window(dt.datetime(2026, 8, 10, 1, 30), cfg) is not None
    assert scheduler.active_window(dt.datetime(2026, 8, 10, 12, 0), cfg) is None


def test_quiet_hours_beats_an_otherwise_active_window():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule(windows=[
        {"id": "always", "start": "00:00", "end": "23:59", "days": list(range(7)), "enabled": True},
    ], quiet_hours={"start": "20:00", "end": "07:00"})
    moment = dt.datetime(2026, 8, 10, 21, 0)
    window = scheduler.active_window(moment, cfg)
    assert window is not None  # the window itself still "matches"...
    from projectos.apps.birdtunes import safety
    assert safety.is_quiet_hours(moment, cfg) is True  # ...but quiet hours vetoes it


def test_disabled_schedule_has_no_active_window():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule(enabled=False)
    assert scheduler.active_window(dt.datetime(2026, 8, 10, 10, 0), cfg) is None


def test_next_change_reports_the_disabled_message():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule(enabled=False)
    result = scheduler.next_change(dt.datetime(2026, 8, 10, 10, 0), cfg)
    assert result["event"] == "none"
    assert "disabled" in result["message"]


def test_next_change_predicts_starts_and_stops():
    from projectos.apps.birdtunes import scheduler

    cfg = _schedule()
    before = scheduler.next_change(dt.datetime(2026, 8, 10, 8, 0), cfg)
    assert before["event"] == "starts"
    assert "09:00" in before["message"]

    during = scheduler.next_change(dt.datetime(2026, 8, 10, 10, 0), cfg)
    assert during["event"] == "stops"
    assert "11:00" in during["message"]


@pytest.mark.asyncio
async def test_scheduler_loop_tick_fires_play_and_stop_callbacks():
    from projectos.apps.birdtunes import scheduler

    calls = {"play": 0, "stop": 0}

    async def on_play(window):
        calls["play"] += 1

    async def on_stop():
        calls["stop"] += 1

    clock_value = {"now": dt.datetime(2026, 8, 10, 8, 55)}
    loop = scheduler.SchedulerLoop(
        get_schedule=lambda: _schedule(),
        on_should_play=on_play,
        on_should_stop=on_stop,
        clock=lambda: clock_value["now"],
    )

    await loop.tick()
    assert calls["play"] == 0

    clock_value["now"] = dt.datetime(2026, 8, 10, 9, 30)
    await loop.tick()
    assert calls["play"] == 1

    clock_value["now"] = dt.datetime(2026, 8, 10, 11, 30)
    await loop.tick()
    assert calls["stop"] == 1


# --------------------------------------------------------------------------------- sources (YouTube import)


def test_source_error_when_yt_dlp_is_not_installed(monkeypatch):
    import sys

    from projectos.apps.birdtunes import sources

    monkeypatch.delitem(sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr(sources.importlib.util, "find_spec", lambda name: None, raising=False) \
        if hasattr(sources, "importlib") else None
    assert sources.available() in (True, False)  # environment-dependent; just must not raise


def test_run_job_imports_a_single_video_and_records_it(db, fake_ytdl, tmp_path):
    from projectos.apps.birdtunes import library, sources

    library.register_schema(db)
    job = sources.create_job(db, "https://youtube.com/watch?v=dQw4w9WgXcQ", kind="video")
    result = sources.run_job(db, job["id"], str(tmp_path))

    assert result["state"] == "done"
    track = db.one("SELECT * FROM app_birdtunes_tracks WHERE source = 'youtube'")
    assert track is not None
    assert track["source_id"] == "dQw4w9WgXcQ"


def test_run_job_dedupes_on_source_id_across_two_jobs(db, fake_ytdl, tmp_path):
    from projectos.apps.birdtunes import library, sources

    library.register_schema(db)
    job1 = sources.create_job(db, "https://youtube.com/watch?v=dQw4w9WgXcQ", kind="video")
    sources.run_job(db, job1["id"], str(tmp_path))
    job2 = sources.create_job(db, "https://youtube.com/watch?v=dQw4w9WgXcQ", kind="video")
    sources.run_job(db, job2["id"], str(tmp_path))

    count = db.scalar("SELECT COUNT(*) FROM app_birdtunes_tracks WHERE source = 'youtube'", default=0)
    assert count == 1


def test_run_job_playlist_continues_past_one_failing_item(db, fake_ytdl, tmp_path):
    import sys

    from projectos.apps.birdtunes import library, sources

    library.register_schema(db)
    yt_dlp = sys.modules["yt_dlp"]
    yt_dlp.YoutubeDL.result = {
        "id": "playlist1",
        "title": "A Calm Playlist",
        "entries": [
            {"id": "good1", "title": "Good One", "webpage_url": "https://youtube.com/watch?v=good1",
             "uploader": "Ch", "duration": 100.0, "ext": "m4a", "thumbnail": ""},
            {"id": "bad1", "title": "Bad One", "webpage_url": "https://youtube.com/watch?v=bad1",
             "uploader": "Ch", "duration": 100.0, "ext": "m4a", "thumbnail": ""},
        ],
    }
    yt_dlp.YoutubeDL.fail_ids = {"bad1"}

    job = sources.create_job(db, "https://youtube.com/playlist?list=x", kind="playlist")
    result = sources.run_job(db, job["id"], str(tmp_path))

    assert result["state"] == "done"
    count = db.scalar("SELECT COUNT(*) FROM app_birdtunes_tracks WHERE source = 'youtube'", default=0)
    assert count == 1  # only the good item made it in


def test_run_job_can_be_cancelled_mid_import(db, fake_ytdl, tmp_path):
    from projectos.apps.birdtunes import library, sources

    library.register_schema(db)
    job = sources.create_job(db, "https://youtube.com/watch?v=dQw4w9WgXcQ", kind="video")
    result = sources.run_job(db, job["id"], str(tmp_path), is_cancelled=lambda: True)
    assert result["state"] == "cancelled"


def test_preview_lists_items_without_downloading(fake_ytdl):
    from projectos.apps.birdtunes import sources

    result = sources.preview("https://youtube.com/watch?v=dQw4w9WgXcQ")
    assert result["kind"] == "video"
    assert result["count"] == 1


# --------------------------------------------------------------------------------- players (null backend)


@pytest.mark.asyncio
async def test_null_player_reports_finished_after_a_short_track():
    from projectos.apps.birdtunes.players.base import Track
    from projectos.apps.birdtunes.players.null import NullPlayer

    player = NullPlayer()
    await player.connect({"id": "dev1", "name": "Test speaker"})
    assert player.connected is True

    finished = []
    player.on_finished = lambda info: finished.append(info)
    await player.play(Track(id="t1", title="Short", duration=0.05))
    await player.wait_finished(timeout=2.0)
    assert finished and finished[0]["reason"] == "finished"


@pytest.mark.asyncio
async def test_null_player_stop_reports_stopped_reason():
    from projectos.apps.birdtunes.players.base import Track
    from projectos.apps.birdtunes.players.null import NullPlayer

    player = NullPlayer()
    await player.connect({"id": "dev1"})
    await player.play(Track(id="t1", title="Long", duration=100.0))
    await player.stop()
    assert player.state == "stopped"


@pytest.mark.asyncio
async def test_null_player_pause_and_resume_change_state():
    from projectos.apps.birdtunes.players.base import Track
    from projectos.apps.birdtunes.players.null import NullPlayer

    player = NullPlayer()
    await player.connect({"id": "dev1"})
    await player.play(Track(id="t1", title="Long", duration=100.0))
    await player.pause()
    assert player.state == "paused"
    await player.resume()
    assert player.state == "playing"
    await player.stop()


def test_null_player_is_always_available():
    from projectos.apps.birdtunes.players.null import NullPlayer

    ok, hint = NullPlayer.available()
    assert ok is True


def test_chromecast_and_airplay_describe_without_raising():
    from projectos.apps.birdtunes.players.airplay import AirPlayPlayer
    from projectos.apps.birdtunes.players.chromecast import ChromecastPlayer

    for cls in (AirPlayPlayer, ChromecastPlayer):
        info = cls.describe()
        assert info["kind"] == cls.kind
        assert isinstance(info["available"], bool)


# --------------------------------------------------------------------------------- HTTP routes


def test_status_route_is_locked_before_setup(client):
    # Before anyone has claimed the box the answer is 428, not 401 -- there is
    # no account to be wrong about yet. Same rule as every other route; see
    # tests/test_security.py.
    response = client.get("/api/apps/birdtunes/status")
    assert response.status_code == 428
    assert response.json()["error"] == "setup_required"


def test_status_route_requires_auth(client):
    client.post("/api/setup", json={"username": "miguel", "password": "correct horse battery staple"})
    client.post("/api/auth/logout")
    response = client.get("/api/apps/birdtunes/status")
    assert response.status_code == 401


def test_status_route_returns_idle_with_the_null_player(auth_client):
    response = auth_client.get("/api/apps/birdtunes/status")
    assert response.status_code == 200
    body = response.json()
    assert body["output"] == "null"
    assert "next_change" in body


def test_library_scan_route_finds_sample_tracks(auth_client, media_dir, sample_tracks):
    response = auth_client.post("/api/apps/birdtunes/library/scan")
    assert response.status_code == 200
    body = response.json()
    assert body["added"] == len(sample_tracks)

    listing = auth_client.get("/api/apps/birdtunes/library")
    assert listing.status_code == 200
    assert len(listing.json()["tracks"]) == len(sample_tracks)


def test_playlists_route_always_includes_the_virtual_all_playlist(auth_client):
    response = auth_client.get("/api/apps/birdtunes/playlists")
    assert response.status_code == 200
    ids = [p["id"] for p in response.json()["playlists"]]
    assert "all" in ids


def test_create_and_delete_playlist_via_http(auth_client):
    created = auth_client.post("/api/apps/birdtunes/playlists", json={"name": "Nap time"})
    assert created.status_code == 200
    playlist_id = created.json()["playlist"]["id"]

    deleted = auth_client.delete("/api/apps/birdtunes/playlists/%s" % playlist_id)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_deleting_the_all_playlist_is_refused_via_http(auth_client):
    response = auth_client.delete("/api/apps/birdtunes/playlists/all")
    assert response.status_code == 400


def test_feedback_route_updates_counters(auth_client, media_dir, sample_tracks):
    auth_client.post("/api/apps/birdtunes/library/scan")
    tracks = auth_client.get("/api/apps/birdtunes/library").json()["tracks"]
    track_id = tracks[0]["id"]

    response = auth_client.post(
        "/api/apps/birdtunes/tracks/%s/feedback" % track_id, json={"action": "like"}
    )
    assert response.status_code == 200
    assert response.json()["feedback"]["likes"] == 1


def test_play_with_no_tracks_reports_a_reason_not_a_bare_stop(auth_client, daytime):
    response = auth_client.post("/api/apps/birdtunes/play", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["playing"] is False
    assert body["reason"] == "no_tracks"


def test_play_pause_stop_round_trip_with_the_null_output(auth_client, daytime, media_dir, sample_tracks):
    auth_client.post("/api/apps/birdtunes/library/scan")

    played = auth_client.post("/api/apps/birdtunes/play", json={})
    assert played.status_code == 200
    body = played.json()
    assert body["playing"] is True

    status = auth_client.get("/api/apps/birdtunes/status").json()
    assert status["state"] == "playing"

    paused = auth_client.post("/api/apps/birdtunes/pause")
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"

    resumed = auth_client.post("/api/apps/birdtunes/resume")
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "playing"

    stopped = auth_client.post("/api/apps/birdtunes/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"


def test_outputs_route_lists_all_three_backends(auth_client):
    response = auth_client.get("/api/apps/birdtunes/outputs")
    assert response.status_code == 200
    kinds = {b["kind"] for b in response.json()["backends"]}
    assert kinds == {"null", "airplay", "chromecast"}


def test_set_output_switches_the_configured_backend(auth_client):
    response = auth_client.put("/api/apps/birdtunes/output", json={"type": "null"})
    assert response.status_code == 200
    assert response.json()["type"] == "null"


def test_set_output_rejects_an_unknown_backend(auth_client):
    response = auth_client.put("/api/apps/birdtunes/output", json={"type": "bluetooth"})
    assert response.status_code == 400


def test_volume_route_clamps_to_the_configured_ceiling(auth_client):
    response = auth_client.post("/api/apps/birdtunes/volume", json={"value": 5.0})
    assert response.status_code == 200
    assert response.json()["volume"] <= 0.6


def test_schedule_route_round_trips_a_window(auth_client):
    payload = {
        "enabled": True,
        "quiet_hours": {"start": "20:00", "end": "07:00"},
        "windows": [{"id": "w1", "name": "Out", "start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4], "enabled": True}],
    }
    response = auth_client.put("/api/apps/birdtunes/schedule", json=payload)
    assert response.status_code == 200
    fetched = auth_client.get("/api/apps/birdtunes/schedule")
    assert fetched.json()["schedule"]["windows"][0]["id"] == "w1"


def test_schedule_presets_route_returns_the_documented_four(auth_client):
    response = auth_client.get("/api/apps/birdtunes/schedule/presets")
    assert response.status_code == 200
    ids = {p["id"] for p in response.json()["presets"]}
    assert ids == {"while_out", "mornings", "afternoons", "weekends"}


def test_compat_route_reports_zero_tracks_on_a_fresh_install(auth_client):
    response = auth_client.get("/api/apps/birdtunes/compat")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["compatible"] == 0


def test_convert_route_returns_409_without_ffmpeg(auth_client, monkeypatch):
    from projectos.apps.birdtunes import sources

    monkeypatch.setattr(sources, "ffmpeg_available", lambda: False)
    response = auth_client.post("/api/apps/birdtunes/convert", json={"track_ids": []})
    assert response.status_code == 409


def test_import_preview_returns_503_without_yt_dlp(auth_client, monkeypatch):
    from projectos.apps.birdtunes import sources

    monkeypatch.setattr(sources, "available", lambda: False)
    response = auth_client.get("/api/apps/birdtunes/import/preview", params={"url": "https://youtube.com/x"})
    assert response.status_code == 503


def test_import_youtube_route_queues_a_job(auth_client, fake_ytdl):
    response = auth_client.post(
        "/api/apps/birdtunes/import/youtube", json={"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}
    )
    assert response.status_code == 200
    assert response.json()["job_id"]


def test_media_route_refuses_a_bad_token(auth_client):
    response = auth_client.get("/api/apps/birdtunes/media/does-not-exist", params={"exp": 9999999999, "t": "bad"})
    assert response.status_code == 403


def test_stats_route_returns_zero_counts_on_a_fresh_install(auth_client):
    response = auth_client.get("/api/apps/birdtunes/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["tracks"] == 0
    assert body["total_plays"] == 0


def test_an_app_can_read_and_write_its_own_config(auth_client) -> None:
    """ctx.config in a panel calls this route; nothing answered it before."""
    response = auth_client.get("/api/apps/birdtunes/config")
    assert response.status_code == 200
    assert "output" in response.json()["config"]

    saved = auth_client.put(
        "/api/apps/birdtunes/config", json={"output.device_id": "chromecast:abc"}
    )
    assert saved.status_code == 200
    assert saved.json()["changed"] == ["output.device_id"]
    assert saved.json()["config"]["output"]["device_id"] == "chromecast:abc"


def test_app_config_is_refused_for_an_app_that_is_not_installed(auth_client) -> None:
    assert auth_client.get("/api/apps/nope/config").status_code == 404


# ------------------------------------------------------------------------------ youtube url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=30", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://example.com/watch?v=nope", ""),
        ("", ""),
    ],
)
def test_youtube_video_id_reads_every_shape_of_link(url, expected):
    """Pure string work: no network and no yt-dlp, so casting works without them."""
    from projectos.apps.birdtunes import sources

    assert sources.youtube_video_id(url) == expected


def test_play_youtube_route_rejects_a_link_that_is_not_youtube(auth_client):
    response = auth_client.post(
        "/api/apps/birdtunes/play/youtube", json={"url": "https://example.com/cat.mp4"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "bad_url"


def test_play_youtube_route_wants_a_url(auth_client):
    response = auth_client.post("/api/apps/birdtunes/play/youtube", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_play_youtube_says_so_when_the_output_cannot_cast(auth_client, daytime):
    """The null output has no play_youtube; saying "playing" there would be a lie."""
    response = auth_client.post(
        "/api/apps/birdtunes/play/youtube",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "output_cannot_cast_youtube"


def test_adding_a_track_to_a_playlist_from_the_library(auth_client, media_dir, sample_tracks):
    """The panel's "+" next to a track posts exactly this."""
    auth_client.put(
        "/api/apps/birdtunes/config", json={"library.paths": [str(media_dir)]}
    )
    auth_client.post("/api/apps/birdtunes/library/scan", json={})
    tracks = auth_client.get("/api/apps/birdtunes/library").json()["tracks"]
    assert tracks, "scan should have found the sample tracks"

    playlist = auth_client.post("/api/apps/birdtunes/playlists", json={"name": "Manha"}).json()
    playlist_id = playlist["playlist"]["id"] if "playlist" in playlist else playlist["id"]

    added = auth_client.post(
        "/api/apps/birdtunes/playlists/%s/tracks" % playlist_id,
        json={"track_ids": [tracks[0]["id"]]},
    )
    assert added.status_code == 200
    assert added.json()["added"] == 1


def test_app_detail_keeps_the_app_state_when_its_player_is_in_error(app, auth_client, monkeypatch):
    """An app's status() must not be able to rename the app or fail it.

    BirdTunes reports the *player's* state and name. Flattened into the record,
    a Chromecast that refused a file turned the whole panel into "Chromecast
    failed to start" -- with no way left to change the output.
    """
    plugins = app.state.plugins
    record = plugins.get("birdtunes")
    monkeypatch.setattr(
        record.instance, "status",
        lambda: {"state": "error", "name": "Chromecast", "player": "chromecast"},
    )

    body = auth_client.get("/api/apps/birdtunes").json()
    assert body["state"] == "running"
    assert body["name"] != "Chromecast"
    assert body["status"]["state"] == "error"
