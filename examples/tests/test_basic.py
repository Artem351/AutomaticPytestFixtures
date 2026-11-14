def test_first(stands):
    # тест получает подготовленные этапы через фикстуру, окружение сам не создаёт
    assert "mock_linux" in stands


def test_second(stands):
    assert stands["mock_power"].params.count == 2
