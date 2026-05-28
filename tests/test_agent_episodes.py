from app.agents import agent_episodes as ep


def test_append_and_read(tmp_path):
    p = tmp_path / "1_episodes.jsonl"
    ep.append_episode(p, run_id=1, trigger_type="cron", summary="did a thing", actions=["telegram_send: hi"])
    ep.append_episode(p, run_id=2, trigger_type="gmail_any_new", summary="new mail from bob", actions=[])
    rows = ep.read_episodes(p)
    assert len(rows) == 2
    assert rows[0]["run_id"] == 1
    assert rows[1]["trigger_type"] == "gmail_any_new"


def test_recall_keyword(tmp_path):
    p = tmp_path / "1_episodes.jsonl"
    ep.append_episode(p, run_id=1, trigger_type="cron", summary="invoice from acme", actions=[])
    ep.append_episode(p, run_id=2, trigger_type="cron", summary="weather report", actions=[])
    hits = ep.recall(p, query="invoice")
    assert len(hits) == 1 and hits[0]["run_id"] == 1


def test_recall_searches_actions(tmp_path):
    p = tmp_path / "1_episodes.jsonl"
    ep.append_episode(p, run_id=1, trigger_type="cron", summary="x", actions=["telegram_send: hackathon reminder"])
    hits = ep.recall(p, query="hackathon")
    assert len(hits) == 1


def test_recall_since_filter(tmp_path):
    p = tmp_path / "1_episodes.jsonl"
    ep._append_raw(p, {"run_id": 1, "ts": "2026-05-01T10:00:00+00:00", "trigger_type": "cron", "summary": "old", "actions": []})
    ep._append_raw(p, {"run_id": 2, "ts": "2026-05-20T10:00:00+00:00", "trigger_type": "cron", "summary": "recent", "actions": []})
    hits = ep.recall(p, query="", since="2026-05-15")
    assert len(hits) == 1 and hits[0]["run_id"] == 2


def test_read_missing_file_returns_empty(tmp_path):
    assert ep.read_episodes(tmp_path / "nope.jsonl") == []


def test_append_is_best_effort_on_bad_dir(tmp_path):
    bad = tmp_path / "afile"
    bad.write_text("x")
    # parent is a file -> mkdir will fail; append must swallow it (no raise)
    ep.append_episode(bad / "sub" / "e.jsonl", run_id=1, trigger_type="cron", summary="s", actions=[])
