"""Ajudantes: pairing, the registry and the job queue.

The queue is the part worth testing hard. It decides who runs what, it has to
survive a helper vanishing mid-job, and every one of its mistakes looks like
"the Pi is slow again" rather than like a bug.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from projectos.core import helpers as core


# --------------------------------------------------------------------------- helpers


def pair_one(db: Any, kind: str = core.KIND_PC, capabilities=None, name: str = "PC do quarto"):
    code = core.new_code(db)["code"]
    return core.pair(
        db,
        code=code,
        name=name,
        kind=kind,
        platform="Windows 11",
        capabilities=capabilities if capabilities is not None else [core.CAP_CPU],
    )


# --------------------------------------------------------------------------- pairing


def test_a_code_is_six_digits_and_single_use(db: Any) -> None:
    code = core.new_code(db)["code"]
    assert len(code) == core.CODE_DIGITS and code.isdigit()

    core.pair(db, code=code, kind=core.KIND_PC)
    with pytest.raises(core.PairingError) as exc:
        core.pair(db, code=code, kind=core.KIND_PC)
    assert exc.value.reason == "code_used"


def test_an_unknown_code_is_refused(db: Any) -> None:
    with pytest.raises(core.PairingError) as exc:
        core.pair(db, code="000000", kind=core.KIND_PC)
    assert exc.value.reason == "invalid_code"


def test_an_expired_code_is_refused(db: Any) -> None:
    code = core.new_code(db, ttl=-5)["code"]
    with pytest.raises(core.PairingError) as exc:
        core.pair(db, code=code, kind=core.KIND_PC)
    assert exc.value.reason == "code_expired"


def test_a_code_minted_for_an_esp32_refuses_a_pc(db: Any) -> None:
    code = core.new_code(db, kind=core.KIND_ESP32)["code"]
    with pytest.raises(core.PairingError) as exc:
        core.pair(db, code=code, kind=core.KIND_PC)
    assert exc.value.reason == "kind_mismatch"


def test_the_token_is_stored_hashed_and_never_returned_again(db: Any) -> None:
    result = pair_one(db)
    token = result["token"]
    stored = db.scalar("SELECT token_hash FROM helpers WHERE id = ?", (result["helper"]["id"],))
    assert stored != token
    assert stored == core.hash_token(token)
    # Nothing that reaches the browser carries it.
    assert "token_hash" not in result["helper"]
    assert "token" not in core.get(db, result["helper"]["id"])


def test_the_token_identifies_its_helper(db: Any) -> None:
    result = pair_one(db)
    found = core.authenticate(db, result["token"])
    assert found is not None and found["id"] == result["helper"]["id"]
    assert core.authenticate(db, "not-a-token") is None
    assert core.authenticate(db, "") is None


def test_a_disabled_helper_stops_authenticating(db: Any) -> None:
    result = pair_one(db)
    core.set_enabled(db, result["helper"]["id"], False)
    assert core.authenticate(db, result["token"]) is None


def test_expired_and_used_codes_are_purged(db: Any) -> None:
    core.new_code(db, ttl=-5)
    used = core.new_code(db)["code"]
    core.pair(db, code=used, kind=core.KIND_PC)
    live = core.new_code(db)["code"]

    core.purge_codes(db)
    remaining = [item["code"] for item in core.open_codes(db)]
    assert remaining == [live]


# --------------------------------------------------------------------------- capabilities


def test_an_esp32_cannot_claim_it_has_a_gpu(db: Any) -> None:
    # Not pedantry: a lie here routes a transcode job to a microcontroller.
    result = pair_one(
        db, kind=core.KIND_ESP32, capabilities=[core.CAP_GPU, core.CAP_SENSOR, "nonsense"]
    )
    assert result["helper"]["capabilities"] == [core.CAP_SENSOR]


def test_capabilities_are_deduplicated_and_lowercased(db: Any) -> None:
    result = pair_one(db, capabilities=["CPU", "cpu", core.CAP_TRANSCODE])
    assert result["helper"]["capabilities"] == [core.CAP_CPU, core.CAP_TRANSCODE]


def test_every_capability_has_a_label(db: Any) -> None:
    missing = [c for c in core.CAPABILITIES if c not in core.CAPABILITY_LABELS]
    assert missing == []


def test_every_kind_declares_what_it_may_claim(db: Any) -> None:
    assert set(core.KIND_CAPABILITIES) == set(core.KINDS)
    for kind, allowed in core.KIND_CAPABILITIES.items():
        assert allowed.issubset(set(core.CAPABILITIES)), kind


# --------------------------------------------------------------------------- registry


def test_a_fresh_helper_counts_as_online_and_a_silent_one_does_not(db: Any) -> None:
    helper = pair_one(db)["helper"]
    assert helper["online"] is True

    db.execute("UPDATE helpers SET last_seen = ? WHERE id = ?", ("2020-01-01T00:00:00Z", helper["id"]))
    assert core.get(db, helper["id"])["online"] is False


def test_a_heartbeat_may_update_facts_and_capabilities(db: Any) -> None:
    # ffmpeg installed on the laptop should not require pairing it again.
    helper = pair_one(db)["helper"]
    updated = core.touch(
        db, helper["id"], facts={"cores": 8}, capabilities=[core.CAP_CPU, core.CAP_TRANSCODE]
    )
    assert updated["facts"] == {"cores": 8}
    assert core.CAP_TRANSCODE in updated["capabilities"]


def test_forgetting_a_helper_leaves_its_work_behind(db: Any) -> None:
    result = pair_one(db)
    helper = result["helper"]
    core.submit(db, "transcode", needs=[core.CAP_CPU])
    job = core.take(db, helper)
    assert job["state"] == core.JOB_RUNNING

    core.forget(db, helper["id"])
    assert core.get(db, helper["id"]) is None
    # The work was still wanted; it goes back in the queue rather than away.
    assert core.get_job(db, job["id"])["state"] == core.JOB_QUEUED


# --------------------------------------------------------------------------- jobs


def test_a_job_goes_only_to_a_helper_that_can_run_it(db: Any) -> None:
    esp = pair_one(db, kind=core.KIND_ESP32, capabilities=[core.CAP_SENSOR], name="esp")["helper"]
    pc = pair_one(db, capabilities=[core.CAP_CPU, core.CAP_TRANSCODE], name="pc")["helper"]
    core.submit(db, "transcode", needs=[core.CAP_TRANSCODE])

    assert core.take(db, esp) is None
    taken = core.take(db, pc)
    assert taken is not None and taken["helper_id"] == pc["id"]


def test_a_job_is_handed_out_once(db: Any) -> None:
    first = pair_one(db, name="a")["helper"]
    second = pair_one(db, name="b")["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])

    assert core.take(db, first) is not None
    assert core.take(db, second) is None


def test_the_oldest_job_goes_first(db: Any) -> None:
    helper = pair_one(db)["helper"]
    first = core.submit(db, "one", needs=[core.CAP_CPU])
    core.submit(db, "two", needs=[core.CAP_CPU])
    assert core.take(db, helper)["id"] == first["id"]


def test_a_job_needing_nothing_runs_anywhere(db: Any) -> None:
    esp = pair_one(db, kind=core.KIND_ESP32, capabilities=[core.CAP_SENSOR])["helper"]
    core.submit(db, "ping")
    assert core.take(db, esp) is not None


def test_finishing_a_job_keeps_its_result(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])
    job = core.take(db, helper)
    done = core.finish(db, job["id"], {"path": "/media/out.mp4"})
    assert done["state"] == core.JOB_DONE
    assert done["result"] == {"path": "/media/out.mp4"}
    assert done["finished_at"]


def test_a_failure_is_retried_then_finally_given_up_on(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])

    for _ in range(core.JOB_MAX_ATTEMPTS - 1):
        job = core.take(db, helper)
        assert job is not None
        assert core.fail(db, job["id"], "boom")["state"] == core.JOB_QUEUED

    job = core.take(db, helper)
    assert core.fail(db, job["id"], "boom")["state"] == core.JOB_FAILED
    # And a dead job stops being offered.
    assert core.take(db, helper) is None


def test_a_job_whose_helper_went_quiet_returns_to_the_queue(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])
    job = core.take(db, helper)
    db.execute(
        "UPDATE helper_jobs SET started_at = ? WHERE id = ?", ("2020-01-01T00:00:00Z", job["id"])
    )

    assert core.requeue_stale(db) == 1
    again = core.get_job(db, job["id"])
    assert again["state"] == core.JOB_QUEUED and again["helper_id"] is None


def test_a_stale_job_that_already_burned_its_attempts_is_failed_not_requeued(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])
    job = core.take(db, helper)
    db.execute(
        "UPDATE helper_jobs SET started_at = ?, attempts = ? WHERE id = ?",
        ("2020-01-01T00:00:00Z", core.JOB_MAX_ATTEMPTS, job["id"]),
    )

    assert core.requeue_stale(db) == 0
    assert core.get_job(db, job["id"])["state"] == core.JOB_FAILED


def test_disconnecting_releases_only_that_helpers_running_jobs(db: Any) -> None:
    first = pair_one(db, name="a")["helper"]
    second = pair_one(db, name="b")["helper"]
    core.submit(db, "one", needs=[core.CAP_CPU])
    core.submit(db, "two", needs=[core.CAP_CPU])
    mine = core.take(db, first)
    theirs = core.take(db, second)

    assert core.release_jobs(db, first["id"]) == 1
    assert core.get_job(db, mine["id"])["state"] == core.JOB_QUEUED
    assert core.get_job(db, theirs["id"])["state"] == core.JOB_RUNNING


def test_cancelling_a_finished_job_does_not_rewrite_history(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])
    job = core.take(db, helper)
    core.finish(db, job["id"], "ok")
    assert core.cancel(db, job["id"])["state"] == core.JOB_DONE


def test_a_cancelled_job_is_never_handed_out(db: Any) -> None:
    helper = pair_one(db)["helper"]
    job = core.submit(db, "work", needs=[core.CAP_CPU])
    core.cancel(db, job["id"])
    assert core.take(db, helper) is None


def test_can_run_answers_from_online_helpers_only(db: Any) -> None:
    result = pair_one(db, capabilities=[core.CAP_CPU, core.CAP_TRANSCODE])
    assert core.can_run(db, [core.CAP_TRANSCODE]) is True
    assert core.can_run(db, [core.CAP_GPU]) is False

    db.execute(
        "UPDATE helpers SET last_seen = ? WHERE id = ?",
        ("2020-01-01T00:00:00Z", result["helper"]["id"]),
    )
    assert core.can_run(db, [core.CAP_TRANSCODE]) is False


def test_a_disabled_helper_is_given_no_work(db: Any) -> None:
    helper = pair_one(db)["helper"]
    core.submit(db, "work", needs=[core.CAP_CPU])
    core.set_enabled(db, helper["id"], False)
    assert core.take(db, core.get(db, helper["id"])) is None


def test_stats_counts_what_the_dashboard_shows(db: Any) -> None:
    pair_one(db, capabilities=[core.CAP_CPU])
    pair_one(db, kind=core.KIND_ESP32, capabilities=[core.CAP_SENSOR], name="esp")
    core.submit(db, "work", needs=[core.CAP_CPU])

    summary = core.stats(db)
    assert summary["helpers"] == 2 and summary["online"] == 2
    assert summary["by_kind"] == {core.KIND_PC: 1, core.KIND_ESP32: 1}
    assert summary["queued"] == 1
    assert set(summary["capabilities"]) == {core.CAP_CPU, core.CAP_SENSOR}


# --------------------------------------------------------------------------- the API


def test_pairing_over_http_returns_the_token_once(auth_client: Any) -> None:
    code = auth_client.post("/api/helpers/codes", json={}).json()["code"]
    response = auth_client.post(
        "/api/helpers/pair",
        json={"code": code, "name": "Notebook", "kind": "pc", "capabilities": ["cpu"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"]
    assert body["helper"]["name"] == "Notebook"

    listing = auth_client.get("/api/helpers").json()
    assert listing["count"] == 1
    assert "token" not in listing["helpers"][0]


def test_a_bad_code_over_http_says_why(auth_client: Any) -> None:
    response = auth_client.post("/api/helpers/pair", json={"code": "123456", "kind": "pc"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_code"


def test_the_registry_needs_a_login(client: Any) -> None:
    client.post("/api/setup", json={"username": "m", "password": "correct horse battery"})
    client.post("/api/auth/logout")
    assert client.get("/api/helpers").status_code == 401


def test_a_job_for_a_capability_nobody_has_is_refused_up_front(auth_client: Any) -> None:
    response = auth_client.post(
        "/api/helpers/jobs", json={"kind": "transcode", "needs": ["telepathy"]}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unknown_capability"


def test_queueing_with_nobody_around_is_allowed_but_says_so(auth_client: Any) -> None:
    response = auth_client.post(
        "/api/helpers/jobs", json={"kind": "transcode", "needs": ["transcode"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job"]["state"] == core.JOB_QUEUED
    assert body["will_run"] is False


def test_the_agent_heartbeat_hands_back_work(auth_client: Any) -> None:
    code = auth_client.post("/api/helpers/codes", json={}).json()["code"]
    token = auth_client.post(
        "/api/helpers/pair", json={"code": code, "kind": "pc", "capabilities": ["cpu"]}
    ).json()["token"]
    auth_client.post("/api/helpers/jobs", json={"kind": "work", "needs": ["cpu"]})

    beat = auth_client.post("/api/helpers/agent/heartbeat", json={"token": token})
    assert beat.status_code == 200, beat.text
    job = beat.json()["job"]
    assert job is not None and job["kind"] == "work"

    done = auth_client.post(
        "/api/helpers/agent/jobs/%s/done" % job["id"],
        params={"token": token},
        json={"result": {"ok": True}},
    )
    assert done.status_code == 200, done.text
    assert done.json()["job"]["state"] == core.JOB_DONE


def test_an_agent_endpoint_refuses_a_bad_token(auth_client: Any) -> None:
    response = auth_client.post("/api/helpers/agent/heartbeat", json={"token": "nope"})
    assert response.status_code == 401
    assert response.json()["error"] == "bad_helper_token"


def test_a_helper_cannot_finish_another_helpers_job(auth_client: Any) -> None:
    tokens = []
    for name in ("a", "b"):
        code = auth_client.post("/api/helpers/codes", json={}).json()["code"]
        tokens.append(
            auth_client.post(
                "/api/helpers/pair",
                json={"code": code, "name": name, "kind": "pc", "capabilities": ["cpu"]},
            ).json()["token"]
        )
    auth_client.post("/api/helpers/jobs", json={"kind": "work", "needs": ["cpu"]})
    job = auth_client.post("/api/helpers/agent/heartbeat", json={"token": tokens[0]}).json()["job"]

    response = auth_client.post(
        "/api/helpers/agent/jobs/%s/done" % job["id"],
        params={"token": tokens[1]},
        json={"result": "stolen"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "not_your_job"


def test_the_agent_link_refuses_an_unknown_token(auth_client: Any) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with auth_client.websocket_connect("/api/helpers/agent/link?token=nope") as socket:
            socket.receive_json()
    assert exc.value.code == 4401


def test_the_agent_link_delivers_a_queued_job(auth_client: Any) -> None:
    code = auth_client.post("/api/helpers/codes", json={}).json()["code"]
    token = auth_client.post(
        "/api/helpers/pair", json={"code": code, "kind": "pc", "capabilities": ["cpu"]}
    ).json()["token"]
    auth_client.post("/api/helpers/jobs", json={"kind": "work", "needs": ["cpu"]})

    with auth_client.websocket_connect("/api/helpers/agent/link?token=%s" % token) as socket:
        first = socket.receive_json()
        assert first["type"] == "hello"
        message = socket.receive_json()
        assert message["type"] == "job" and message["job"]["kind"] == "work"
        socket.send_json({"type": "done", "job": message["job"]["id"], "result": "ok"})

    jobs = auth_client.get("/api/helpers/jobs").json()["jobs"]
    assert jobs[0]["state"] == core.JOB_DONE


def test_renaming_and_forgetting_over_http(auth_client: Any) -> None:
    code = auth_client.post("/api/helpers/codes", json={}).json()["code"]
    helper = auth_client.post(
        "/api/helpers/pair", json={"code": code, "kind": "pc", "capabilities": ["cpu"]}
    ).json()["helper"]

    renamed = auth_client.patch("/api/helpers/%s" % helper["id"], json={"name": "Torre"})
    assert renamed.json()["helper"]["name"] == "Torre"

    assert auth_client.delete("/api/helpers/%s" % helper["id"]).json()["ok"] is True
    assert auth_client.get("/api/helpers/%s" % helper["id"]).status_code == 404
