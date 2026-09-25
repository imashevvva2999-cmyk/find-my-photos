"""The whole interface is in Russian: pages, buttons and the messages the server sends back."""
import re

from conftest import create_event

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_pages_are_in_russian(admin, client):
    _, token = create_event(admin, "Технокадр")
    visitor = client.get(f"/e/{token}").text
    assert '<html lang="ru">' in visitor and "Технокадр" in visitor
    for phrase in ("Найти мои фото", "Сделать фото", "Загрузить фото", "Даю согласие", "Все фотографии", "Конфиденциальность"):
        assert phrase in visitor, phrase
    assert "Ваши мероприятия" in admin.get("/admin").text
    assert "Вход для организатора" in client.get("/admin/login").text
    assert "Ссылка недействительна" in client.get("/e/not-a-real-token-at-all-000").text


def test_server_messages_are_in_russian(admin, client):
    _, token = create_event(admin)
    r = client.post(f"/e/{token}/search", content=b"not an image", headers={"content-type": "image/jpeg"})
    assert r.status_code == 422 and CYRILLIC.search(r.json()["message"])
    r = client.post("/e/not-a-real-token-at-all-000/search", content=b"x")
    assert r.status_code == 404 and CYRILLIC.search(r.json()["message"])
    r = client.post("/admin/login", data={"password": "wrong"})
    assert "Неверный пароль" in r.text
