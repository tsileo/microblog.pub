from fastapi.testclient import TestClient

from app.main import app


def test_admin_endpoints_are_authenticated(client: TestClient):
    routes_tested = []

    for route in app.routes:
        if not route.path.startswith("/admin") or route.path == "/admin/login":
            continue

        for method in route.methods:
            resp = client.request(method, route.path)

            # Admin routes should redirect to the login page
            assert resp.status_code == 302, f"{method} {route.path} is unauthenticated"
            assert resp.headers.get("Location") == "http://testserver/admin/login"
            routes_tested.append((method, route.path))

    assert len(routes_tested) > 0
