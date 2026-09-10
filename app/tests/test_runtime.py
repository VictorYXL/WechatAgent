from wechat_agent.runtime import ServiceLease, request_stop, service_running


def test_service_lock_and_stop_are_scoped_and_released(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert request_stop(first) == "not_running"
    with ServiceLease(first) as lease:
        assert service_running(first)
        assert not service_running(second)
        assert request_stop(first) == "starting_retry"
        lease.publish()
        assert not lease.stop_requested()
        assert request_stop(first) == "stop_requested"
        assert lease.stop_requested()
    assert not service_running(first)
    assert not (first / "service-state.json").exists()
    with ServiceLease(first) as next_lease:
        next_lease.publish()
        assert not next_lease.stop_requested()


def test_stale_process_metadata_does_not_stop_another_process(tmp_path):
    (tmp_path / "service-state.json").write_text('{"pid": 1, "instance": "old"}', encoding="utf-8")
    assert request_stop(tmp_path) == "not_running"