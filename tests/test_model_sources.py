from __future__ import annotations

import io
import json
import urllib.error

import pytest

from app.config_store import ConfigStore
from app.main import app
from app.model_sources import tokens
from app.model_sources.tokens import (
    CIVITAI_ME_URL,
    CIVITAI_SECRET,
    ModelSourceError,
    check_civitai_token,
    civitai_state,
    civitai_token,
    clear_civitai_token,
    save_civitai_token,
)


@pytest.fixture
def store(tmp_path):
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setitem(app.config, "CONFIG_STORE", store)
    return app.test_client()


def _file_token(store: ConfigStore):
    if not store.path.exists():
        return None
    return json.loads(store.path.read_text(encoding="utf-8")).get("comfyui", {}).get("civitai_api_token")


def test_token_lives_in_the_credential_store(store, model_source_secrets):
    assert civitai_token(store) == ""
    assert save_civitai_token(store, "  civitai-secret  ") == "system"
    assert model_source_secrets.values[CIVITAI_SECRET] == "civitai-secret"
    assert civitai_token(store) == "civitai-secret"
    assert _file_token(store) is None
    assert civitai_state(store)["saved"] is True and civitai_state(store)["storage"] == "system"

    clear_civitai_token(store)
    assert civitai_token(store) == "" and CIVITAI_SECRET not in model_source_secrets.values
    assert civitai_state(store) | {"secret_store": None} == {"saved": False, "storage": None, "secret_store": None}


def test_token_from_an_older_config_moves_to_the_credential_store(store, model_source_secrets):
    store.update_comfyui_settings(civitai_api_token="legacy-token")
    assert civitai_token(store) == "legacy-token"
    assert model_source_secrets.values[CIVITAI_SECRET] == "legacy-token"
    assert _file_token(store) is None
    assert civitai_token(store) == "legacy-token"


def test_without_a_credential_store_the_config_keeps_the_token(store, model_source_secrets):
    model_source_secrets.available = False
    assert save_civitai_token(store, "plain-token") == "config"
    assert _file_token(store) == "plain-token"
    assert civitai_token(store) == "plain-token"
    assert civitai_state(store)["storage"] == "config"

    # Once a credential store appears the token moves there.
    model_source_secrets.available = True
    assert civitai_token(store) == "plain-token"
    assert model_source_secrets.values[CIVITAI_SECRET] == "plain-token" and _file_token(store) is None


def test_saving_an_empty_token_is_refused(store):
    with pytest.raises(ModelSourceError) as error:
        save_civitai_token(store, "   ")
    assert error.value.code == "token_empty"


class _Opener:
    def __init__(self, *, body=None, code=None, error=None):
        self.body, self.code, self.error = body, code, error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.code:
            raise urllib.error.HTTPError(request.full_url, self.code, "refused", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(self.body).encode("utf-8"))


def test_check_asks_civitai_whose_token_it_is():
    opener = _Opener(body={"username": "lotargo", "email": "someone@example.com", "isMember": False})
    assert check_civitai_token(" tok ", opener=opener) == {"username": "lotargo", "member": False}
    sent = opener.requests[0]
    assert sent.full_url == CIVITAI_ME_URL and sent.get_header("Authorization") == "Bearer tok"

    with pytest.raises(ModelSourceError) as rejected:
        check_civitai_token("bad", opener=_Opener(code=401))
    assert rejected.value.code == "token_rejected"
    with pytest.raises(ModelSourceError) as offline:
        check_civitai_token("tok", opener=_Opener(error=urllib.error.URLError("offline")))
    assert offline.value.code == "civitai_unreachable"


def test_routes_save_check_and_remove_without_showing_the_token(client, store, monkeypatch):
    accounts = {"good": {"username": "lotargo", "member": True}}

    def fake_check(token, opener=None):
        if token == "offline":
            raise ModelSourceError("Cannot reach Civitai: offline", code="civitai_unreachable")
        if token not in accounts:
            raise ModelSourceError("Civitai did not accept this token.", code="token_rejected")
        return accounts[token]

    monkeypatch.setattr("app.model_sources.routes.check_civitai_token", fake_check)

    assert client.get("/api/model-sources").get_json()["civitai"]["saved"] is False
    assert client.post("/api/model-sources/civitai", json={"token": ""}).status_code == 400

    refused = client.post("/api/model-sources/civitai", json={"token": "bad"})
    assert refused.status_code == 400 and refused.get_json()["code"] == "token_rejected"
    assert civitai_token(store) == ""

    saved = client.post("/api/model-sources/civitai", json={"token": "good"})
    body = saved.get_json()
    assert saved.status_code == 200 and body["account"]["username"] == "lotargo" and body["warning"] is None
    assert body["civitai"]["saved"] is True and "good" not in saved.get_data(as_text=True)
    assert client.post("/api/model-sources/civitai/check").get_json()["account"]["username"] == "lotargo"

    unchecked = client.post("/api/model-sources/civitai", json={"token": "offline"}).get_json()
    assert unchecked["account"] is None and "Saved without checking" in unchecked["warning"]
    assert civitai_token(store) == "offline"

    assert client.delete("/api/model-sources/civitai").get_json()["civitai"]["saved"] is False
    assert client.post("/api/model-sources/civitai/check").status_code == 404


def test_token_routes_answer_only_this_computer(client):
    remote = {"REMOTE_ADDR": "192.168.1.20"}
    assert client.post("/api/model-sources/civitai", json={"token": "x"}, environ_base=remote).status_code == 403
    assert client.delete("/api/model-sources/civitai", environ_base=remote).status_code == 403
    assert client.post("/api/model-sources/civitai/check", environ_base=remote).status_code == 403


def test_comfyui_settings_keep_the_token_when_the_field_is_empty(client, store):
    save_civitai_token(store, "kept-token")
    # The workflow editor always sends the field, empty unless the user typed a token.
    assert client.post("/api/comfyui/config", json={"civitai_api_token": "", "host": "127.0.0.1"}).status_code == 200
    assert civitai_token(store) == "kept-token"
    assert client.get("/api/comfyui/config").get_json()["has_civitai_token"] is True

    client.post("/api/comfyui/config", json={"civitai_api_token": "new-token"})
    assert civitai_token(store) == "new-token"
    assert tokens.secret_store().get(CIVITAI_SECRET) == "new-token"
