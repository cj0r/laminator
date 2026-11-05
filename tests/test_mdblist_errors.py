"""An MDBList failure must never put the API key (a query parameter) into a message, a job log or the UI."""
import httpx
import pytest

from laminator import actions
from laminator.mdblist import MDBList, MDBListError

KEY = "SECRETKEY-0123456789abcdef"


def _with(handler):
    m = MDBList(KEY, __import__("pathlib").Path("/nonexistent-cache-dir"), ttl_hours=0)
    m.http = httpx.Client(transport=httpx.MockTransport(handler))
    return m


@pytest.mark.parametrize("status,words", [(429, "limit is used up"), (403, "rejected the API key"), (401, "rejected the API key"), (503, "HTTP 503")])
def test_a_failed_lookup_says_what_happened_without_the_key(status, words):
    m = _with(lambda r: httpx.Response(status, json={}, request=r))
    with pytest.raises(MDBListError) as e:
        m.lookup("imdb", "movie", "tt1")
    assert words in str(e.value) and KEY not in str(e.value) and KEY not in repr(e.value) and e.value.status == status
    assert e.value.__cause__ is None and e.value.__suppress_context__                  # the original error (whose text holds the URL) is not chained
    with pytest.raises(MDBListError) as e2:
        m.check()
    assert KEY not in str(e2.value)


def test_unknown_titles_are_still_just_none_and_network_errors_are_clean():
    assert _with(lambda r: httpx.Response(404, request=r)).lookup("imdb", "movie", "tt1") is None
    def boom(r): raise httpx.ConnectError("refused", request=r)
    with pytest.raises(MDBListError, match="could not reach MDBList") as e:
        _with(boom).lookup("imdb", "movie", "tt1")
    assert KEY not in str(e.value)


def test_a_run_that_hits_the_limit_stops_with_the_plain_message_and_no_key_in_the_job(tmp_path):
    from laminator.web.jobs import JobManager
    jm = JobManager(tmp_path)
    m = _with(lambda r: httpx.Response(429, json={}, request=r))
    it = type("It", (), {"id": "1", "type": "Movie", "provider_ids": {"imdb": "tt1"}})()
    def work():
        actions.choose_score(it, m, {"sources": ["aggregate"]}, {})
        yield from ()
    job = jm.start("scores", work)
    for _ in range(200):
        if job.status in ("done", "failed"):
            break
        __import__("time").sleep(0.02)
    assert job.status == "failed" and "limit is used up" in (job.error or "") and KEY not in (job.error or "")
