def test_can_change_state(power, service, system):
    assert system.is_ready()
    assert power.is_on()
    assert service.is_running()
    power.set_on(False)
    service.stop()
    assert not power.is_on()
    assert not service.is_running()


def test_initial_state_is_restored(power, service):
    assert power.is_on()
    assert service.is_running()
