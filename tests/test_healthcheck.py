def test_healthcheck_is_fast_and_public(client):
    response = client.get("/healthz")

    assert response.status_code == 204
    assert response.data == b""
